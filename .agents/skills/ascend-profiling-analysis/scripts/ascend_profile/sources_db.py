#!/usr/bin/env python3
"""db-direct input source: rebuild the ``kernel_details.csv`` event stream
directly from ``ascend_pytorch_profiler_*.db``.

The torch_npu profiler stores every device task in a sqlite db; the classic
``kernel_details.csv`` is a downstream projection of that db produced by the
closed-source msprof exporter.  This module reconstructs the same event
stream — same row union (compute kernels + one ``hcom_*`` row and one
``*AicpuKernel`` row per communication op), same 46 columns, same
start-time ordering — so normalize can consume the db directly and skip the
CSV export entirely.

All mapping rules (required schema, STRING_IDS field maps, PMU metric ->
column pivot, comm-row constants, documented semantic adoptions) live in
``knowledge/db_source_mapping.yaml`` and are loaded at runtime, mirroring
``rules.py``'s treatment of ``kernel_signatures.yaml``.

Row reconstruction notes (golden-pair verified):
  * ``COMPUTE_TASK_INFO`` joins 1:1 to ``TASK`` on ``globalTaskId``.  MIX
    kernels have two TASK rows (AIC/AIV parts); only the part referenced by
    COMPUTE_TASK_INFO is exported, which the join reproduces exactly.
  * ``TASK_PMU_INFO.globalTaskId`` references ``COMPUTE_TASK_INFO.rowid``
    (not ``COMPUTE_TASK_INFO.globalTaskId``).
  * ``Start Time(us)`` / ``Duration(us)`` are exact integer-ns decimal
    insertions, never float formatting.
  * Fields the exporter computes from pre-calibration raw device data
    (``Wait Time(us)``, aicpu ``Block Num``) cannot be bit-reproduced from
    the db; the adapter emits the db-native semantics documented under
    ``documented_differences`` in the mapping file.
"""

from __future__ import annotations

import bisect
import functools
import sqlite3
from array import array
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

try:
    from .sources import KernelRowAccessor
    from .store import KNOWLEDGE_DIR
except ImportError:  # pragma: no cover - script-mode fallback
    from sources import KernelRowAccessor  # type: ignore[no-redef]
    from store import KNOWLEDGE_DIR  # type: ignore[no-redef]


_MAPPING_PATH = KNOWLEDGE_DIR / "db_source_mapping.yaml"

KIND_COMPUTE = 0
KIND_COMM_HCOM = 1
KIND_COMM_AICPU = 2

# Upper bound on the flat PMU pivot array (slots, i.e. doubles).  100M slots
# at 26 metrics/row covers ~3.8M compute rows (~800MB); anything larger is a
# corrupt or unexpected db and fails closed instead of exhausting memory.
_PMU_MAX_SLOTS = 100_000_000


class DbSourceError(RuntimeError):
    """Raised when a profiler db cannot be consumed as a kernel event source."""


# ----------------------------------------------------------------------------
# Mapping knowledge file: load + strict validation
# ----------------------------------------------------------------------------


def _schema_error(path: Path, context: str, problem: str) -> RuntimeError:
    return RuntimeError(f"{path.name}: {context}: {problem}")


def _require_str_list(path: Path, context: str, value: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty) or not all(isinstance(item, str) and item for item in value):
        raise _schema_error(path, context, "must be a non-empty list of strings")
    return list(value)


def _validate_mapping(path: Path, doc: Mapping[str, Any]) -> dict[str, Any]:
    if doc.get("version") != 1:
        raise _schema_error(path, "version", "must be 1")

    probe = doc.get("schema_probe")
    if not isinstance(probe, Mapping) or not isinstance(probe.get("required_tables"), Mapping):
        raise _schema_error(path, "schema_probe", "required_tables missing or not a mapping")
    required_tables: dict[str, list[str]] = {}
    for table, columns in probe["required_tables"].items():
        required_tables[str(table)] = _require_str_list(path, f"schema_probe.required_tables.{table}", columns)
    optional_tables: dict[str, list[str]] = {}
    raw_optional = probe.get("optional_tables") or {}
    if not isinstance(raw_optional, Mapping):
        raise _schema_error(path, "schema_probe.optional_tables", "must be a mapping when present")
    for table, columns in raw_optional.items():
        optional_tables[str(table)] = _require_str_list(path, f"schema_probe.optional_tables.{table}", columns)

    csv_header = tuple(_require_str_list(path, "csv_header", doc.get("csv_header")))
    if len(set(csv_header)) != len(csv_header):
        raise _schema_error(path, "csv_header", "duplicate column names")
    # Columns the emitter fills mechanically (not via YAML field maps) must
    # exist in the declared header, otherwise row assembly raises KeyError.
    mechanical = ("Device_id", "Model ID", "Task ID", "Stream ID", "Name", "Type", "Accelerator Core",
                  "Start Time(us)", "Duration(us)", "Wait Time(us)", "Context ID")
    missing_mechanical = [column for column in mechanical if column not in csv_header]
    if missing_mechanical:
        raise _schema_error(path, "csv_header", f"missing mechanical columns: {missing_mechanical}")

    sentinels = doc.get("sentinels")
    if not isinstance(sentinels, Mapping) or not isinstance(sentinels.get("invalid_id"), int) or not isinstance(sentinels.get("not_available"), str):
        raise _schema_error(path, "sentinels", "needs integer invalid_id and string not_available")

    kernel_task_types = _require_str_list(path, "kernel_task_types", doc.get("kernel_task_types"))

    row_kinds = doc.get("row_kinds")
    if not isinstance(row_kinds, Mapping):
        raise _schema_error(path, "row_kinds", "section missing or not a mapping")
    compute = row_kinds.get("compute")
    if not isinstance(compute, Mapping) or not isinstance(compute.get("string_fields"), Mapping) or not isinstance(compute.get("int_fields"), Mapping):
        raise _schema_error(path, "row_kinds.compute", "needs string_fields and int_fields mappings")
    for section, key in (("string_fields", compute["string_fields"]), ("int_fields", compute["int_fields"])):
        for column, cti_col in key.items():
            if column not in csv_header or not isinstance(cti_col, str) or not cti_col:
                raise _schema_error(path, f"row_kinds.compute.{section}", f"bad entry {column!r}: {cti_col!r}")
    comm_sections: dict[str, dict[str, str]] = {}
    for kind_name in ("comm_hcom", "comm_aicpu"):
        section = row_kinds.get(kind_name)
        if not isinstance(section, Mapping):
            raise _schema_error(path, f"row_kinds.{kind_name}", "section missing or not a mapping")
        constants = section.get("constants") or {}
        if not isinstance(constants, Mapping):
            raise _schema_error(path, f"row_kinds.{kind_name}.constants", "must be a mapping")
        for column, value in constants.items():
            if column not in csv_header or not isinstance(value, str):
                raise _schema_error(path, f"row_kinds.{kind_name}.constants", f"bad entry {column!r}: {value!r}")
        if section.get("pmu_columns") != sentinels["not_available"]:
            raise _schema_error(path, f"row_kinds.{kind_name}.pmu_columns", f"must be {sentinels['not_available']!r}")
        comm_sections[kind_name] = {str(column): str(value) for column, value in constants.items()}
        core = section.get("accelerator_core")
        if kind_name == "comm_hcom" and (not isinstance(core, str) or not core):
            raise _schema_error(path, "row_kinds.comm_hcom.accelerator_core", "must be a non-empty string")

    pmu_metrics = doc.get("pmu_metrics")
    if not isinstance(pmu_metrics, Mapping) or not pmu_metrics:
        raise _schema_error(path, "pmu_metrics", "section missing or not a mapping")
    pmu_defs: list[dict[str, Any]] = []
    for column, spec in pmu_metrics.items():
        context = f"pmu_metrics.{column}"
        if column not in csv_header:
            raise _schema_error(path, context, f"{column!r} not in csv_header")
        if not isinstance(spec, Mapping) or not isinstance(spec.get("metric"), str) or not spec["metric"]:
            raise _schema_error(path, context, "needs a non-empty string metric")
        fmt = spec.get("format")
        if fmt == "int":
            pmu_defs.append({"column": column, "metric": spec["metric"], "format": "int"})
        else:
            scale, decimals = spec.get("scale"), spec.get("decimals")
            if not isinstance(scale, (int, float)) or not isinstance(decimals, int) or decimals < 0:
                raise _schema_error(path, context, "needs numeric scale and non-negative integer decimals")
            pmu_defs.append({"column": column, "metric": spec["metric"], "format": "scaled", "scale": float(scale), "decimals": decimals})

    derived = doc.get("derived_metrics")
    if not isinstance(derived, Mapping):
        raise _schema_error(path, "derived_metrics", "section missing or not a mapping")
    derived_defs: dict[str, dict[str, Any]] = {}
    for column, spec in derived.items():
        context = f"derived_metrics.{column}"
        if column not in csv_header:
            raise _schema_error(path, context, f"{column!r} not in csv_header")
        if not isinstance(spec, Mapping) or spec.get("formula") != "aic_time_per_duration_per_total_cores" or not isinstance(spec.get("numerator_metric"), str):
            raise _schema_error(path, context, "only formula aic_time_per_duration_per_total_cores with a numerator_metric is supported")
        decimals = spec.get("decimals")
        if not isinstance(decimals, int) or decimals < 0:
            raise _schema_error(path, context, "decimals must be a non-negative integer")
        total_cores = spec.get("total_cores")
        if not isinstance(total_cores, int) or total_cores <= 0:
            raise _schema_error(path, context, "total_cores must be a positive integer")
        block_column = spec.get("block_num_column")
        if block_column not in csv_header:
            raise _schema_error(path, context, f"block_num_column {block_column!r} not in csv_header")
        if block_column not in compute["int_fields"]:
            raise _schema_error(path, context, f"block_num_column {block_column!r} must be one of row_kinds.compute.int_fields")
        derived_defs[str(column)] = {
            "formula": spec["formula"],
            "numerator_metric": spec["numerator_metric"],
            "block_num_column": block_column,
            "total_cores": total_cores,
            "decimals": decimals,
        }

    wait_time = doc.get("wait_time")
    if not isinstance(wait_time, Mapping) or wait_time.get("rule") != "idle_gap_previous_exported_end":
        raise _schema_error(path, "wait_time", "only rule idle_gap_previous_exported_end is supported")

    differences = doc.get("documented_differences") or []
    if not isinstance(differences, list) or not all(isinstance(item, Mapping) and item.get("field") for item in differences):
        raise _schema_error(path, "documented_differences", "must be a list of mappings with a field")

    return {
        "csv_header": csv_header,
        "required_tables": required_tables,
        "optional_tables": optional_tables,
        "invalid_id": sentinels["invalid_id"],
        "not_available": sentinels["not_available"],
        "kernel_task_types": tuple(kernel_task_types),
        "compute_string_fields": {str(column): str(cti_col) for column, cti_col in compute["string_fields"].items()},
        "compute_int_fields": {str(column): str(cti_col) for column, cti_col in compute["int_fields"].items()},
        "comm_hcom_constants": comm_sections["comm_hcom"],
        "comm_hcom_core": str(row_kinds["comm_hcom"]["accelerator_core"]),
        "comm_aicpu_constants": comm_sections["comm_aicpu"],
        "pmu_defs": tuple(pmu_defs),
        "derived_defs": derived_defs,
        "documented_differences": [dict(item) for item in differences],
    }


@functools.lru_cache(maxsize=4)
def load_db_mapping(path: str | Path | None = None) -> dict[str, Any]:
    """Load and strictly validate ``knowledge/db_source_mapping.yaml``."""

    mapping_path = Path(path) if path is not None else _MAPPING_PATH
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - yaml is a hard dependency
        raise RuntimeError("PyYAML is required to load knowledge/db_source_mapping.yaml") from exc
    if not mapping_path.exists():
        raise RuntimeError(f"db source mapping knowledge base missing: {mapping_path}")
    doc = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, Mapping):
        raise _schema_error(mapping_path, "root", "must be a mapping")
    return _validate_mapping(mapping_path, doc)


# ----------------------------------------------------------------------------
# Schema probe (fail-closed gate for normalize --source auto)
# ----------------------------------------------------------------------------


def probe_db_schema(db_path: str | Path, *, mapping: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Check whether ``db_path`` carries every table/column the adapter needs.

    Returns ``{"path", "ok", "missing", "error"}`` — never raises, so the
    normalize auto mode can fail closed on any db it cannot consume.
    """

    result: dict[str, Any] = {"path": str(db_path), "ok": False, "missing": {}, "error": None}
    try:
        mapping = mapping or load_db_mapping()
    except RuntimeError as exc:
        result["error"] = str(exc)
        return result
    try:
        con = _connect(db_path)
    except (sqlite3.Error, DbSourceError) as exc:
        result["error"] = f"open failed: {exc}"
        return result
    try:
        present_tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing: dict[str, list[str]] = {}
        for table, columns in mapping["required_tables"].items():
            if table not in present_tables:
                missing[table] = list(columns)
                continue
            present_cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            absent = [column for column in columns if column not in present_cols]
            if absent:
                missing[table] = absent
        # Optional tables (e.g. COMMUNICATION_* — absent on TP1 captures with
        # no HCCL) never gate ``ok``; their absence is reported so the caller
        # can note that the db legitimately contributes zero comm rows.
        optional_missing: dict[str, list[str]] = {}
        for table, columns in (mapping.get("optional_tables") or {}).items():
            if table not in present_tables:
                optional_missing[table] = list(columns)
                continue
            present_cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            absent = [column for column in columns if column not in present_cols]
            if absent:
                optional_missing[table] = absent
        result["missing"] = missing
        result["optional_missing"] = optional_missing
        result["ok"] = not missing
    except sqlite3.Error as exc:
        result["error"] = f"probe failed: {exc}"
    finally:
        con.close()
    return result


# ----------------------------------------------------------------------------
# Row reconstruction
# ----------------------------------------------------------------------------


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise DbSourceError(f"profiler db not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def _ns_to_us_text(ns: int) -> str:
    """Exact integer-ns -> us decimal insertion (torch_npu convert_ns2us_str)."""

    text = str(ns)
    if len(text) <= 3:
        return "0." + text.zfill(3)
    return text[:-3] + "." + text[-3:]


class _Runtime:
    """Mapping + per-db resolved state (STRING_IDS, metric slots, formats)."""

    def __init__(self, mapping: Mapping[str, Any], string_ids: Mapping[int, str]) -> None:
        self.mapping = mapping
        self.string_ids = string_ids
        self.header: tuple[str, ...] = mapping["csv_header"]
        self.col_index = {column: idx for idx, column in enumerate(self.header)}
        self.na = mapping["not_available"]
        # PMU metric name -> slot, resolved through STRING_IDS (value -> id).
        id_by_value: dict[str, int] = {}
        for str_id, value in string_ids.items():
            id_by_value.setdefault(value, str_id)
        self.pmu_defs = mapping["pmu_defs"]
        self.metric_slots: dict[int, int] = {}
        for slot, spec in enumerate(self.pmu_defs):
            str_id = id_by_value.get(spec["metric"])
            if str_id is not None:
                self.metric_slots[str_id] = slot
        self.numerator_slot: dict[str, int] = {}
        for column, spec in mapping["derived_defs"].items():
            for slot, pmu_spec in enumerate(self.pmu_defs):
                if pmu_spec["metric"] == spec["numerator_metric"]:
                    self.numerator_slot[column] = slot
                    break
            else:  # pragma: no cover - validated indirectly via pmu_metrics
                raise DbSourceError(f"derived metric {column!r} references unknown pmu metric {spec['numerator_metric']!r}")
        int_field_names = list(mapping["compute_int_fields"].keys())
        self.derived_block_idx: dict[str, int] = {
            column: int_field_names.index(spec["block_num_column"])
            for column, spec in mapping["derived_defs"].items()
        }

    def text(self, str_id: Any) -> str:
        """STRING_IDS resolution with N/A fallback for NULL/missing ids."""

        if str_id is None:
            return self.na
        return self.string_ids.get(str_id, self.na)


def _load_string_ids(con: sqlite3.Connection) -> dict[int, str]:
    return {row[0]: row[1] for row in con.execute("SELECT id, value FROM STRING_IDS")}


def _collect_records(con: sqlite3.Connection, rt: _Runtime) -> list[tuple]:
    """Pull all three row kinds as compact sortable records.

    Uniform record shape: ``(startNs, kind, (taskId, sub_id), payload)``.
    ``sub_id`` is the source-row identifier used only as a deterministic
    tie-breaker (globalTaskId / rowid).
    """

    records: list[tuple] = []

    # Optional comm tables are absent on captures without HCCL (e.g. TP1);
    # then the adapter simply contributes zero comm rows.
    present_tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    compute_cols = rt.mapping["compute_string_fields"]
    int_cols = rt.mapping["compute_int_fields"]
    cti_select = ["c.rowid", "c.globalTaskId", "t.startNs", "t.endNs", "t.deviceId", "t.streamId", "t.taskId", "t.modelId", "t.contextId"]
    cti_select += [f"c.{cti_col}" for cti_col in list(compute_cols.values()) + list(int_cols.values())]
    query = f"SELECT {', '.join(cti_select)} FROM COMPUTE_TASK_INFO c JOIN TASK t ON t.globalTaskId = c.globalTaskId"
    n_string = len(compute_cols)
    for row in con.execute(query):
        (cti_rowid, global_task_id, start_ns, end_ns, device_id, stream_id, task_id, model_id, context_id) = row[:9]
        values = row[9:]
        strings = tuple(rt.text(value) for value in values[:n_string])
        ints = tuple(value for value in values[n_string:])
        payload = (end_ns, device_id, stream_id, task_id, model_id, context_id, cti_rowid, strings, ints)
        records.append((start_ns, KIND_COMPUTE, (task_id or 0, global_task_id or 0), payload))

    if "COMMUNICATION_OP" in present_tables:
        for rowid, op_name, op_type, start_ns, end_ns, device_id in con.execute(
            "SELECT rowid, opName, opType, startNs, endNs, deviceId FROM COMMUNICATION_OP"
        ):
            payload = (end_ns, device_id, rt.text(op_name), rt.text(op_type))
            records.append((start_ns, KIND_COMM_HCOM, (0, rowid), payload))

    if "COMMUNICATION_SCHEDULE_TASK_INFO" in present_tables and "TASK" in present_tables:
        for row in con.execute(
            "SELECT s.globalTaskId, s.name, s.opType, s.taskType, "
            "t.startNs, t.endNs, t.deviceId, t.streamId, t.taskId, t.modelId, t.contextId "
            "FROM COMMUNICATION_SCHEDULE_TASK_INFO s JOIN TASK t ON t.globalTaskId = s.globalTaskId"
        ):
            (global_task_id, name, op_type, task_type, start_ns, end_ns, device_id, stream_id, task_id, model_id, context_id) = row
            payload = (end_ns, device_id, stream_id, task_id, model_id, context_id, rt.text(name), rt.text(op_type), rt.text(task_type))
            records.append((start_ns, KIND_COMM_AICPU, (task_id or 0, global_task_id or 0), payload))

    return records


def _load_pmu(con: sqlite3.Connection, rt: _Runtime) -> tuple[array, int]:
    """Pivot TASK_PMU_INFO into a flat double array indexed by rowid * slots + slot.

    A flat ``array('d')`` keeps 8.8M materialized metric values at ~70MB and
    makes no assumptions about row ordering or contiguity.  Metrics without a
    CSV column (``aiv_mac_*``, ``aiv_mte1_*``) are dropped here.
    """

    slot_count = len(rt.pmu_defs)
    max_gtid = con.execute("SELECT MAX(globalTaskId) FROM TASK_PMU_INFO").fetchone()[0]
    if max_gtid is None:
        return array("d"), slot_count
    if max_gtid < 0 or (max_gtid + 1) * slot_count > _PMU_MAX_SLOTS:
        raise DbSourceError(f"TASK_PMU_INFO.globalTaskId upper bound {max_gtid} exceeds supported range")
    values = array("d", bytes((max_gtid + 1) * slot_count * 8))
    metric_slots = rt.metric_slots
    for gtid, name_id, value in con.execute("SELECT globalTaskId, name, value FROM TASK_PMU_INFO"):
        slot = metric_slots.get(name_id)
        if slot is not None and value is not None:
            values[gtid * slot_count + slot] = value
    return values, slot_count


def _build_wait_indexes(records: Sequence[tuple]) -> tuple[dict[Any, list[int]], list[int]]:
    """End-time indexes for the idle-gap Wait Time rule.

    Per-stream ends cover exported streamed rows (compute + aicpu); the
    device-wide list additionally covers hcom comm ops.  All lists are
    sorted once so each row's previous end is one bisect.
    """

    stream_ends: dict[Any, list[int]] = {}
    all_ends: list[int] = []
    for _start_ns, kind, _tie, payload in records:
        end_ns = payload[0]
        all_ends.append(end_ns)
        if kind != KIND_COMM_HCOM:
            stream_ends.setdefault(payload[2], []).append(end_ns)
    for ends in stream_ends.values():
        ends.sort()
    all_ends.sort()
    return stream_ends, all_ends


def _idle_gap_us(start_ns: int, sorted_ends: Sequence[int]) -> float:
    pos = bisect.bisect_right(sorted_ends, start_ns) - 1
    if pos < 0:
        return 0.0
    return max(0.0, (start_ns - sorted_ends[pos]) / 1000.0)


def _format_pmu_cells(rt: _Runtime, pmu: array, slot_count: int, cti_rowid: int, duration_ns: int, int_values: Sequence[Any]) -> Iterator[str]:
    # A compute row without any PMU rows has a rowid beyond the pivot array;
    # its cells emit zeros, matching the exporter's rendering of idle pipes.
    base = cti_rowid * slot_count
    in_range = bool(pmu) and 0 <= base and base + slot_count <= len(pmu)
    for slot, spec in enumerate(rt.pmu_defs):
        value = pmu[base + slot] if in_range else 0.0
        if spec["format"] == "int":
            yield str(int(value))
        else:
            yield str(round(value * spec["scale"], spec["decimals"]))
    for column, spec in rt.mapping["derived_defs"].items():
        slot = rt.numerator_slot[column]
        numerator = pmu[base + slot] if in_range else 0.0
        block_num = int_values[rt.derived_block_idx[column]] or 0
        ratio = (numerator * block_num / (duration_ns * spec["total_cores"]) * 100.0) if duration_ns > 0 else 0.0
        yield f"{ratio:.{spec['decimals']}f}"


def _emit_rows(records: list[tuple], pmu: array, slot_count: int, rt: _Runtime) -> Iterator[tuple[int, tuple[str, ...]]]:
    mapping = rt.mapping
    na = rt.na
    invalid_id = mapping["invalid_id"]
    hcom_constants = mapping["comm_hcom_constants"]
    aicpu_constants = mapping["comm_aicpu_constants"]
    string_fields = mapping["compute_string_fields"]
    int_fields = mapping["compute_int_fields"]
    pmu_column_names = tuple(spec["column"] for spec in rt.pmu_defs) + tuple(mapping["derived_defs"].keys())
    col = rt.col_index
    stream_ends, all_ends = _build_wait_indexes(records)

    records.sort(key=lambda rec: (rec[0], rec[1], rec[2]))
    for row_idx, (start_ns, kind, _tie, payload) in enumerate(records):
        cells = [na] * len(rt.header)
        end_ns = payload[0]
        duration_ns = max(0, end_ns - start_ns)
        cells[col["Device_id"]] = str(payload[1])
        cells[col["Start Time(us)"]] = _ns_to_us_text(start_ns)
        cells[col["Duration(us)"]] = _ns_to_us_text(duration_ns)

        if kind == KIND_COMPUTE:
            (_end, _dev, stream_id, task_id, model_id, context_id, cti_rowid, strings, ints) = payload
            cells[col["Model ID"]] = str(model_id)
            cells[col["Task ID"]] = str(task_id)
            cells[col["Stream ID"]] = str(stream_id)
            cells[col["Context ID"]] = na if context_id == invalid_id else str(context_id)
            for (column, _cti_col), value in zip(string_fields.items(), strings):
                cells[col[column]] = value
            for (column, _cti_col), value in zip(int_fields.items(), ints):
                cells[col[column]] = na if value is None else str(value)
            wait_us = _idle_gap_us(start_ns, stream_ends.get(stream_id, ()))
            for column, text in zip(pmu_column_names, _format_pmu_cells(rt, pmu, slot_count, cti_rowid, duration_ns, ints)):
                cells[col[column]] = text
        elif kind == KIND_COMM_HCOM:
            (_end, _dev, name, op_type) = payload
            cells[col["Name"]] = name
            cells[col["Type"]] = op_type
            cells[col["Accelerator Core"]] = mapping["comm_hcom_core"]
            for column, value in hcom_constants.items():
                cells[col[column]] = value
            wait_us = _idle_gap_us(start_ns, all_ends)
        else:  # KIND_COMM_AICPU
            (_end, _dev, stream_id, task_id, model_id, context_id, name, op_type, task_type) = payload
            cells[col["Model ID"]] = str(model_id)
            cells[col["Task ID"]] = str(task_id)
            cells[col["Stream ID"]] = str(stream_id)
            cells[col["Context ID"]] = na if context_id == invalid_id else str(context_id)
            cells[col["Name"]] = name
            cells[col["Type"]] = op_type
            cells[col["Accelerator Core"]] = task_type
            for column, value in aicpu_constants.items():
                cells[col[column]] = value
            wait_us = _idle_gap_us(start_ns, stream_ends.get(stream_id, ()))

        cells[col["Wait Time(us)"]] = f"{wait_us:.6f}"
        yield row_idx, tuple(cells)


def iter_db_rows(db_path: str | Path, *, mapping: Mapping[str, Any] | None = None) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Yield ``(row_idx, cells)`` with cells as strings in ``csv_header`` order.

    Rows are sorted by ``Start Time(us)`` (startNs) with a deterministic
    ``(kind, taskId, id)`` tie-break, mirroring the kernel_details.csv row
    order.  The connection closes once the generator is exhausted.
    """

    mapping = mapping or load_db_mapping()
    con = _connect(db_path)
    try:
        rt = _Runtime(mapping, _load_string_ids(con))
        records = _collect_records(con, rt)
        pmu, slot_count = _load_pmu(con, rt)
    finally:
        con.close()
    yield from _emit_rows(records, pmu, slot_count, rt)


def iter_kernel_events_from_db(db_path: str | Path, *, mapping: Mapping[str, Any] | None = None) -> Iterator[tuple[int, dict[str, str]]]:
    """Dict-row view over :func:`iter_db_rows`, keys = kernel_details.csv columns.

    Same ``(row_idx, row)`` contract as ``store.iter_csv_rows``, so any
    consumer written for the CSV path can read the db path unchanged.
    """

    header = (mapping or load_db_mapping())["csv_header"]
    for row_idx, cells in iter_db_rows(db_path, mapping=mapping):
        yield row_idx, dict(zip(header, cells))


def db_row_reader(
    db_path: str | Path,
    *,
    mapping: Mapping[str, Any] | None = None,
) -> tuple[KernelRowAccessor, Iterator[tuple[int, tuple[str, ...]]]]:
    """Positional twin of ``normalize._kernel_row_reader`` for the db source.

    Returns a ``KernelRowAccessor`` resolved against the canonical
    kernel_details.csv header plus the ``(row_idx, cells)`` iterator, so the
    normalize row loop runs byte-identically over both sources.
    """

    mapping = mapping or load_db_mapping()
    return KernelRowAccessor(mapping["csv_header"]), iter_db_rows(db_path, mapping=mapping)
