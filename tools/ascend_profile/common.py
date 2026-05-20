#!/usr/bin/env python3
"""Shared utilities for the Ascend profiling analysis framework."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from xml.sax.saxutils import escape


SCHEMA_VERSION = 1
TOOL_VERSION = "ascend-profile-analysis-0.1"
SHAPE_SIGNATURE_DIM_SAMPLE_LIMIT = 32
SPREADSHEET_COLUMN_BASE = 26
csv.field_size_limit(1024 * 1024 * 1024)


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    kind: str
    path: str
    sha256: str | None = None
    rank_id: str | None = None
    row_base: str = "zero_based"
    row_start: int | None = None
    row_end: int | None = None


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    profile_id: str
    rank_id: str
    source_id: str
    row_idx: int
    name_raw: str
    task_type: str
    accelerator_core: str
    stream_id: str
    start_us: float
    end_us: float
    duration_us: float
    wait_us: float
    op_categories: tuple[str, ...] = ()
    op_roles: tuple[str, ...] = ()
    shape_signature: str | None = None
    shape_features: dict[str, Any] = field(default_factory=dict)
    raw_fields_ref: SourceRef | None = None
    # Per-event pipeline breakdown read directly from kernel_details.csv
    # extended columns. All values are absolute microseconds within the
    # event's duration. Empty dict when the source CSV does not expose the
    # corresponding columns (older CANN profilers); we never fabricate.
    pipeline_us: dict[str, float] = field(default_factory=dict)
    # Canonical op type derived from the ``Accelerator Core`` column plus
    # AIV-signal heuristic. Range:
    # ``aic | aiv | mix_cv | mix_comm_aiv | communication | aicpu | dsa | unknown``.
    # See ``op_type_from_event`` for the rules.
    op_type: str = "unknown"


@dataclass(frozen=True)
class StepSegment:
    segment_id: str
    rank_id: str
    segment_type: str
    complete: bool
    row_start: int
    row_end: int
    start_us: float
    end_us: float
    cluster_id: str | None = None
    step_family: str | None = None
    main_layer_count: int | None = None
    speculative_layer_count: int | None = None
    structure_signature: str | None = None
    layer_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayerSegment:
    layer_id: str
    rank_id: str
    segment_id: str
    layer_index: int
    layer_role: str
    boundary_source: str
    row_start: int
    row_end: int
    start_us: float
    end_us: float
    structure_signature: str | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlockSegment:
    """Sub-layer block: ``attention``, ``ffn``, or ``moe``.

    A vLLM transformer layer typically decomposes into one ``attention``
    block followed by one ``ffn`` (dense FFN) or ``moe`` block.  Layers
    without any attention kernel are flagged as ``companion_layer`` -- e.g.
    the eager-mode bookkeeping passes that run alongside a graph-mode
    forward, or sampling-only layers in the speculative head.

    The block boundary is derived strictly from event roles inside the
    parent ``LayerSegment``, never from a name heuristic, so the
    decomposition stays evidence-grade.
    """

    block_id: str
    rank_id: str
    segment_id: str
    layer_id: str
    layer_index: int
    block_index: int
    block_kind: str
    companion_layer: bool
    row_start: int
    row_end: int
    start_us: float
    end_us: float
    event_count: int = 0
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructureObservation:
    structure_id: str
    scope_type: str
    rank_id: str
    role: str
    role_family: str
    confidence: str
    segment_id: str | None = None
    layer_id: str | None = None
    implementation_evidence: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    kind: str
    summary: str
    source_refs: tuple[SourceRef, ...] = ()
    event_ids: tuple[str, ...] = ()
    segment_ids: tuple[str, ...] = ()
    layer_ids: tuple[str, ...] = ()
    alignment_ids: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrossRankAlignment:
    alignment_id: str
    alignment_type: str
    rank_ids: tuple[str, ...]
    segment_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    start_us: float | None = None
    end_us: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosisFinding:
    claim_id: str
    claim_type: str
    summary: str
    confidence: str
    finding_type: str
    scope: str
    severity: str = "info"
    rank_ids: tuple[str, ...] = ()
    alignment_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Interval:
    start_us: float
    end_us: float

    @property
    def duration_us(self) -> float:
        return max(0.0, self.end_us - self.start_us)


@dataclass(frozen=True)
class BusySegment:
    start_us: float
    end_us: float
    first_event: NormalizedEvent
    last_event: NormalizedEvent

    @property
    def duration_us(self) -> float:
        return max(0.0, self.end_us - self.start_us)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_id(prefix: str, *parts: Any, length: int = 16) -> str:
    text = "\x1f".join(str(part) for part in parts)
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()[:length]
    return f"{prefix}_{digest}"


def to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_plain(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(to_plain(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(str(key))
                fieldnames.append(str(key))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def iter_csv_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_idx, row in enumerate(reader):
            yield row_idx, row


def csv_value(value: Any) -> Any:
    value = to_plain(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(block_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


_PICK_KEY_CACHE: dict[tuple[str, ...], dict[str, str]] = {}


def pick(row: Mapping[str, Any], keys: Sequence[str], default: str = "") -> str:
    row_keys = tuple(str(key) for key in row.keys())
    lowered = _PICK_KEY_CACHE.get(row_keys)
    if lowered is None:
        lowered = {key.strip().lower(): key for key in row_keys}
        _PICK_KEY_CACHE[row_keys] = lowered
    for key in keys:
        actual = lowered.get(key.strip().lower())
        if actual is None:
            continue
        value = str(row.get(actual, "")).strip()
        if value:
            return value
    return default


def try_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def fold_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def row_ranges(values: Iterable[int]) -> list[list[int]]:
    ordered = sorted(set(int(value) for value in values))
    if not ordered:
        return []
    ranges: list[list[int]] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append([start, prev])
        start = prev = value
    ranges.append([start, prev])
    return ranges


def quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    left = math.floor(pos)
    right = math.ceil(pos)
    if left == right:
        return float(ordered[left])
    weight = pos - left
    return float(ordered[left] * (1 - weight) + ordered[right] * weight)


def infer_rank_id(rank_dir: Path, ordinal: int) -> str:
    text = rank_dir.name
    if re.search(r"^(rank|device)[_-]?\d+_.+_ascend_pt$", text, flags=re.IGNORECASE):
        return re.sub(r"[^A-Za-z0-9_]+", "_", text).lower()
    patterns = [
        r"(dp\d+_pp\d+_tp\d+_dcp\d+_ep\d+_rank\d+)",
        r"(rank[_-]?\d+)",
        r"(device[_-]?\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return re.sub(r"[^A-Za-z0-9_]+", "_", match.group(1)).lower()
    return f"rank_{ordinal}"


def discover_rank_dirs(root: Path) -> list[Path]:
    root = root.resolve()
    if root.is_file() and root.name == "kernel_details.csv":
        return [root.parent.parent if root.parent.name == "ASCEND_PROFILER_OUTPUT" else root.parent]
    if (root / "kernel_details.csv").is_file() or (root / "ASCEND_PROFILER_OUTPUT" / "kernel_details.csv").is_file():
        return [root]
    candidates: set[Path] = set()
    for path in root.rglob("kernel_details.csv"):
        parent = path.parent.parent if path.parent.name == "ASCEND_PROFILER_OUTPUT" else path.parent
        candidates.add(parent)
    return sorted(candidates, key=lambda item: str(item))


def kernel_details_path(rank_dir: Path) -> Path | None:
    direct = rank_dir / "kernel_details.csv"
    if direct.is_file():
        return direct
    nested = rank_dir / "ASCEND_PROFILER_OUTPUT" / "kernel_details.csv"
    if nested.is_file():
        return nested
    matches = sorted(rank_dir.glob("**/kernel_details.csv"))
    return matches[0] if matches else None


def supplemental_sources(rank_dir: Path) -> list[tuple[str, Path]]:
    patterns = [
        ("trace_view_json", "**/trace_view.json"),
        ("op_summary_csv", "**/op_summary*.csv"),
        ("communication_json", "**/communication.json"),
    ]
    out: list[tuple[str, Path]] = []
    for kind, pattern in patterns:
        for path in sorted(rank_dir.glob(pattern)):
            out.append((kind, path))
    return out


_SHAPE_COLUMN_CACHE: dict[tuple[str, ...], tuple[str, ...]] = {}


def shape_signature(row: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    row_keys = tuple(str(key) for key in row.keys())
    shape_columns = _SHAPE_COLUMN_CACHE.get(row_keys)
    if shape_columns is None:
        lowered = _PICK_KEY_CACHE.get(row_keys)
        if lowered is None:
            lowered = {key.strip().lower(): key for key in row_keys}
            _PICK_KEY_CACHE[row_keys] = lowered
        shape_columns = tuple(
            lowered[key.lower()]
            for key in ("Input Shapes", "Input Shape", "Input", "Output Shapes", "Output Shape", "Output")
            if key.lower() in lowered
        )
        _SHAPE_COLUMN_CACHE[row_keys] = shape_columns
    if not shape_columns:
        return None, {}
    shape_text = " ".join(str(row.get(key, "")).strip() for key in shape_columns).strip()
    if not shape_text:
        return None, {}
    dims = [int(value) for value in re.findall(r"-?\d+", shape_text)]
    positive_dims = [value for value in dims if value > 0]
    features: dict[str, Any] = {
        "dims": positive_dims[:SHAPE_SIGNATURE_DIM_SAMPLE_LIMIT],
        "dim_count": len(positive_dims),
    }
    if positive_dims:
        features["max_dim"] = max(positive_dims)
        features["min_dim"] = min(positive_dims)
        features["first_dim"] = positive_dims[0]
        features["last_dim"] = positive_dims[-1]
    digest = hashlib.blake2b(shape_text.encode("utf-8"), digest_size=8).hexdigest()
    return f"shape_{digest}", features


def task_type_from_row(row: Mapping[str, Any]) -> str:
    return pick(row, ("Task Type", "Kernel Type", "Type"), "UNKNOWN").upper()


def core_from_row(row: Mapping[str, Any]) -> str:
    return pick(row, ("Accelerator Core", "Core Type", "Task Type", "Kernel Type", "Type"), "UNKNOWN").upper()


def name_from_row(row: Mapping[str, Any]) -> str:
    return pick(row, ("Name", "Op Name", "Kernel Name", "Operation Name"), "UNKNOWN")


def stream_from_row(row: Mapping[str, Any]) -> str:
    return pick(row, ("Stream ID", "StreamId", "Stream", "stream_id"), "unknown")


def event_time_from_row(row: Mapping[str, Any]) -> tuple[float, float, float, float]:
    start = try_float(pick(row, ("Start Time(us)", "Start Time", "Start(us)", "Start", "ts"), "0"))
    duration = try_float(pick(row, ("Duration(us)", "Duration", "dur"), "0"))
    wait = try_float(pick(row, ("Wait Time(us)", "Wait Time", "Wait(us)", "wait"), "0"))
    end = try_float(pick(row, ("End Time(us)", "End Time", "End(us)", "End"), "0"))
    if end <= start:
        end = start + max(0.0, duration)
    if duration <= 0 and end > start:
        duration = end - start
    return start, end, max(0.0, duration), max(0.0, wait)


# CANN msprof / kernel_details.csv pipeline column names.  We keep the
# original CANN nomenclature (just drop the "(us)" suffix to make valid
# Python identifiers) so every aggregated metric stays one-to-one with the
# raw evidence column.  No fabrication: if a column is missing the value
# stays out of the dict.
#
# IMPORTANT: AI Core (cube) and AI Vector use a decoupled pipeline on
# Atlas A2/A3, so ``aic_mte2_time`` (GM/L1 -> L0A/L0B for the matmul/cube
# unit) and ``aiv_mte2_time`` (GM -> UB for the vector unit) MUST stay
# separate -- merging them masks the actual bottleneck.
_PIPELINE_SOURCE_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("aicore_time",      ("aicore_time(us)", "aicore_time", "AI Core Time(us)")),
    ("aiv_time",         ("aiv_time(us)", "aiv_time", "AI Vector Time(us)")),
    ("aic_mac_time",     ("aic_mac_time(us)", "aic_mac_time")),
    ("aic_fixpipe_time", ("aic_fixpipe_time(us)", "aic_fixpipe_time")),
    ("aic_mte1_time",    ("aic_mte1_time(us)", "aic_mte1_time")),
    ("aic_mte2_time",    ("aic_mte2_time(us)", "aic_mte2_time")),
    ("aic_scalar_time",  ("aic_scalar_time(us)", "aic_scalar_time")),
    ("aiv_vec_time",     ("aiv_vec_time(us)", "aiv_vec_time")),
    ("aiv_mte2_time",    ("aiv_mte2_time(us)", "aiv_mte2_time")),
    ("aiv_mte3_time",    ("aiv_mte3_time(us)", "aiv_mte3_time")),
    ("aiv_scalar_time",  ("aiv_scalar_time(us)", "aiv_scalar_time")),
)


# The full pipeline schema downstream stages are allowed to assume.  Order
# matters for column layout in operator_summary.csv; CANN convention is
# AIC stages first, AIV stages second.
PIPELINE_FIELDS: tuple[str, ...] = tuple(key for key, _ in _PIPELINE_SOURCE_COLUMNS)


# Stage groups for bound-class derivation.  ``aicore_time`` and
# ``aiv_time`` are totals and intentionally NOT in any group -- they're
# just the per-core wall time.
_AIC_STAGES: tuple[str, ...] = ("aic_mac_time", "aic_fixpipe_time", "aic_mte1_time", "aic_mte2_time", "aic_scalar_time")
_AIV_STAGES: tuple[str, ...] = ("aiv_vec_time", "aiv_mte2_time", "aiv_mte3_time", "aiv_scalar_time")
_PIPELINE_STAGES: tuple[str, ...] = _AIC_STAGES + _AIV_STAGES


_BOUND_FAMILY_BY_STAGE: dict[str, str] = {
    "aic_mac_time":     "cube",
    "aic_fixpipe_time": "cube",
    "aic_mte1_time":    "aic_mte",
    "aic_mte2_time":    "aic_mte",
    "aic_scalar_time":  "scalar",
    "aiv_vec_time":     "vector",
    "aiv_mte2_time":    "aiv_mte",
    "aiv_mte3_time":    "aiv_mte",
    "aiv_scalar_time":  "scalar",
}


def pipeline_breakdown_from_row(row: Mapping[str, Any]) -> dict[str, float]:
    """Extract per-event pipeline times from a kernel_details.csv row.

    Returns an empty dict when no source column is present, so callers can
    detect missing-data cases without fabricating zeros.  Otherwise the
    returned dict maps each ``PIPELINE_FIELDS`` key to a float in
    microseconds.
    """

    out: dict[str, float] = {}
    for key, candidates in _PIPELINE_SOURCE_COLUMNS:
        text = pick(row, candidates, "")
        if not text:
            continue
        out[key] = round(max(0.0, try_float(text)), 6)
    return out


def has_pipeline_signal(pipeline: Mapping[str, Any] | None) -> bool:
    """Return True iff the pipeline dict carries any non-zero stage value.

    The two ``*_time`` totals (``aicore_time`` / ``aiv_time``) are
    excluded -- on a true zero-compute event we may still have a non-zero
    total, so the stage breakdown is the authoritative signal.
    """

    if not pipeline:
        return False
    for key in _PIPELINE_STAGES:
        if float(pipeline.get(key) or 0.0) > 0.0:
            return True
    return False


def sum_pipeline_breakdown(pipelines: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Sum a sequence of pipeline dicts using the original CANN field names.

    Returns an empty dict if no input dict carries any stage signal, so
    callers can preserve ``unknown`` semantics downstream.
    """

    totals: dict[str, float] = {key: 0.0 for key in PIPELINE_FIELDS}
    seen = False
    for pipeline in pipelines:
        if not pipeline:
            continue
        for key in PIPELINE_FIELDS:
            value = pipeline.get(key)
            if value is None:
                continue
            totals[key] += float(value)
        if has_pipeline_signal(pipeline):
            seen = True
    if not seen:
        return {}
    return {key: round(value, 6) for key, value in totals.items()}


_OP_TYPE_BY_CORE: dict[str, str] = {
    "AI_CORE":         "aic",
    "AICORE":          "aic",
    "AI_VECTOR_CORE":  "aiv",
    "AIVECTOR":        "aiv",
    "AI_VECTORCORE":   "aiv",
    "MIX_AIC":         "mix_cv",
    "MIX_AIV":         "mix_cv",
    "MIX_AICAIV":      "mix_cv",
    "MIX_AIC_AIV":     "mix_cv",
    "COMMUNICATION":   "communication",
    "AI_CPU":          "aicpu",
    "AICPU":           "aicpu",
    "DSA_SQE":         "dsa",
}


def op_type_from_event(
    accelerator_core: str | None,
    pipeline: Mapping[str, Any] | None = None,
) -> str:
    """Classify an event into the canonical op_type taxonomy.

    The ``Accelerator Core`` column is the source of truth for whether an
    op runs on AIC, AIV, or both.  We only fall back to pipeline signal
    when the column is absent or unrecognised.

    Special case: a ``COMMUNICATION`` core with non-zero AIV stage time
    indicates a fused comm + AIV kernel (e.g. ``DispatchFFNCombine``,
    ``MoeDistributeDispatch``, ``MoeDistributeCombine``).  Those need a
    distinct label so the report can analyse the AIV burden separately
    from the pure HCCL portion.
    """

    core = (accelerator_core or "").strip().upper()
    base = _OP_TYPE_BY_CORE.get(core)
    if base is None:
        if not core:
            return "unknown"
        if "MIX" in core:
            return "mix_cv"
        if "COMM" in core or "HCCL" in core:
            return "communication"
        if "VECTOR" in core or core.endswith("_AIV"):
            return "aiv"
        if "CORE" in core:
            return "aic"
        return "unknown"

    if base == "communication":
        # Detect fused comm + AIV (dispatch / combine / distribute style ops).
        aiv_signal = 0.0
        if pipeline:
            aiv_signal = float(pipeline.get("aiv_time") or 0.0)
            if aiv_signal <= 0.0:
                for key in _AIV_STAGES:
                    aiv_signal += float(pipeline.get(key) or 0.0)
        if aiv_signal > 0.0:
            return "mix_comm_aiv"
    return base


def bound_class_from_pipeline(
    pipeline: Mapping[str, Any] | None,
    *,
    op_type: str | None = None,
    is_aicpu: bool = False,
    is_communication: bool = False,
    mixed_margin: float = 0.10,
) -> dict[str, Any]:
    """Classify an op-level pipeline aggregate.

    Returns a dict with four keys:
      * ``bound_stage`` -- the single stage (or short-circuit label) with
        the largest cumulative time.  For ``mix_comm_aiv`` only the AIV
        stages are considered (the AIC side of a comm-fused op is not
        meaningful work).
      * ``bound_family`` -- coarser bucket in
        ``{cube, vector, aic_mte, aiv_mte, scalar, mixed, aicpu,
           communication, comm_aiv_mix, dsa, unknown}``.
        ``mixed`` means the top stage is within ``mixed_margin`` of the
        runner-up's family share.  ``comm_aiv_mix`` is a hard-set label
        so the report can group dispatch/combine kernels together.
      * ``dominant_core`` -- ``aic`` / ``aiv`` / ``mix`` / ``none`` based
        on stage-time totals.  ``mix`` for any op_type ``mix_cv`` whose
        stages cover both AIC and AIV with comparable weight.
      * ``op_type`` -- echoed back for convenience so callers don't have
        to thread it separately.

    The decoupled-architecture rule is enforced by deriving the family
    from per-stage time, never from a merged compute-vs-MTE ratio: AIC
    mte2 stalls and AIV mte2 stalls land in different families
    (``aic_mte`` vs ``aiv_mte``) so the report can call them out
    separately.
    """

    op_type_resolved = op_type or "unknown"

    if is_aicpu or op_type_resolved == "aicpu":
        return {"bound_stage": "aicpu", "bound_family": "aicpu", "dominant_core": "none", "op_type": "aicpu"}
    if op_type_resolved == "dsa":
        return {"bound_stage": "dsa", "bound_family": "dsa", "dominant_core": "none", "op_type": "dsa"}
    if op_type_resolved == "communication" or (is_communication and op_type_resolved not in {"mix_comm_aiv"}):
        return {"bound_stage": "communication", "bound_family": "communication", "dominant_core": "none", "op_type": "communication"}

    pipeline = pipeline or {}

    if op_type_resolved == "mix_comm_aiv":
        aiv_us = {key: float(pipeline.get(key) or 0.0) for key in _AIV_STAGES}
        if sum(aiv_us.values()) <= 0:
            return {"bound_stage": "communication", "bound_family": "comm_aiv_mix", "dominant_core": "none", "op_type": "mix_comm_aiv"}
        bound_stage = max(aiv_us, key=aiv_us.get)
        return {"bound_stage": bound_stage, "bound_family": "comm_aiv_mix", "dominant_core": "aiv", "op_type": "mix_comm_aiv"}

    if not has_pipeline_signal(pipeline):
        return {"bound_stage": "unknown", "bound_family": "unknown", "dominant_core": "none", "op_type": op_type_resolved}

    stage_us: dict[str, float] = {key: float(pipeline.get(key) or 0.0) for key in _PIPELINE_STAGES}
    total = sum(stage_us.values())
    if total <= 0:
        return {"bound_stage": "unknown", "bound_family": "unknown", "dominant_core": "none", "op_type": op_type_resolved}

    bound_stage = max(stage_us, key=stage_us.get)
    family_total: dict[str, float] = {}
    for stage, value in stage_us.items():
        family_total[_BOUND_FAMILY_BY_STAGE[stage]] = family_total.get(_BOUND_FAMILY_BY_STAGE[stage], 0.0) + value
    sorted_families = sorted(family_total.items(), key=lambda item: item[1], reverse=True)
    top_family, top_value = sorted_families[0]
    runner_value = sorted_families[1][1] if len(sorted_families) > 1 else 0.0
    if total > 0 and (top_value - runner_value) / total < mixed_margin:
        bound_family = "mixed"
    else:
        bound_family = top_family

    aic_total = sum(stage_us[key] for key in _AIC_STAGES)
    aiv_total = sum(stage_us[key] for key in _AIV_STAGES)
    if aic_total <= 0 and aiv_total <= 0:
        dominant_core = "none"
    elif aic_total <= 0:
        dominant_core = "aiv"
    elif aiv_total <= 0:
        dominant_core = "aic"
    elif abs(aic_total - aiv_total) / max(aic_total, aiv_total) < mixed_margin:
        dominant_core = "mix"
    elif aic_total > aiv_total:
        dominant_core = "aic"
    else:
        dominant_core = "aiv"

    return {"bound_stage": bound_stage, "bound_family": bound_family, "dominant_core": dominant_core, "op_type": op_type_resolved}


_CATEGORY_ROLE_CACHE: dict[tuple[str, str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {}


def categories_and_roles(name: str, task_type: str, accelerator_core: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    cache_key = (name, task_type, accelerator_core)
    cached = _CATEGORY_ROLE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    text = fold_text(f"{name} {task_type} {accelerator_core}")
    categories: set[str] = set()
    roles: set[str] = set()
    if any(token in text for token in ("hccl", "hcom", "allreduce", "allgather", "reducescatter", "alltoall")):
        categories.add("communication.collective")
        roles.add("communication")
    if "metadata" in text and any(token in text for token in ("sparseattnsharedkv", "sparseattentionsharedkv", "sharedkv")):
        categories.add("attention.csa.metadata")
        roles.add("attention_aux")
    elif any(token in text for token in ("fusedinferattentionscore", "unpadflashattention", "flashattention")):
        categories.add("attention.gqa_or_mha")
        roles.add("attention")
    if "metadata" not in text and any(token in text for token in ("sparseattnsharedkv", "sparseattentionsharedkv", "sharedkv")):
        categories.add("attention.csa")
        roles.add("attention")
    if "mla" in text and "matmul" not in text:
        categories.add("attention.mla")
        roles.add("attention")
    if any(token in text for token in ("causalconv", "causalconv1d", "mamba", "deltanet", "gdn")):
        categories.add("attention.linear_or_mamba")
        roles.add("attention")
    if "attention" in text and not any(cat.startswith("attention.") for cat in categories):
        categories.add("attention.generic")
        roles.add("attention")
    if any(token in text for token in ("moegating", "gatingtopk", "topkgating", "topkrouter")):
        categories.add("moe.gating")
        roles.add("moe")
    if any(token in text for token in ("dispatchffncombine", "ffncombine")):
        categories.add("moe.dispatch_expert_compute")
        roles.add("moe")
    if any(token in text for token in ("moedispatch", "dispatch")) and "dispatchffncombine" not in text:
        categories.add("moe.dispatch")
        roles.add("moe")
    if "combine" in text and "dispatchffncombine" not in text:
        categories.add("moe.combine")
        roles.add("moe")
    if any(token in text for token in ("groupedmatmul", "batchmatmul", "quantbatchmatmul", "matmul", "gemm", "gmm")):
        categories.add("compute.matmul")
        roles.add("compute")
    if "argmax" in text:
        categories.add("sampling_or_selection")
        roles.add("selection")
    if "compressor" in text or "compress" in text:
        categories.add("attention.csa.compressor")
        roles.add("attention_aux")
    if "indexer" in text or "lightningindex" in text:
        categories.add("attention.csa.indexer")
        roles.add("attention_aux")
    is_collective = "communication.collective" in categories
    if (text.startswith("hc") and not is_collective) or "mhc" in text:
        categories.add("block_head.mhc_prefix")
        roles.add("block_head")
    if "norm" in text:
        categories.add("normalization")
        roles.add("normalization")
        if "add" in text or "mhc" in text or text.startswith("hc"):
            categories.add("block_head")
            roles.add("block_head")
    if any(token in text for token in ("aicpu", "ai_cpu")):
        categories.add("aicpu")
        roles.add("aicpu")
    result = (tuple(sorted(categories)), tuple(sorted(roles)))
    _CATEGORY_ROLE_CACHE[cache_key] = result
    return result


def is_aicpu_event(event: NormalizedEvent) -> bool:
    text = f"{event.task_type} {event.accelerator_core} {' '.join(event.op_categories)}".lower()
    return "aicpu" in text or "ai_cpu" in text


def is_comm_event(event: NormalizedEvent) -> bool:
    return "communication" in event.op_roles or "communication.collective" in event.op_categories


def is_ai_core_like(event: NormalizedEvent) -> bool:
    text = f"{event.task_type} {event.accelerator_core}".upper()
    if is_aicpu_event(event) or is_comm_event(event):
        return False
    return any(token in text for token in ("AI_CORE", "AICORE", "AI_VECTOR", "AIVECTOR", "MIX_AIC", "MIXAIC"))


def merge_event_segments(events: Sequence[NormalizedEvent]) -> list[BusySegment]:
    ordered = sorted(
        (event for event in events if event.end_us > event.start_us),
        key=lambda item: (item.start_us, item.end_us, item.row_idx),
    )
    if not ordered:
        return []
    segments: list[BusySegment] = []
    start = ordered[0].start_us
    end = ordered[0].end_us
    first_event = ordered[0]
    last_event = ordered[0]
    for event in ordered[1:]:
        if event.start_us <= end:
            if event.end_us > end or (math.isclose(event.end_us, end) and event.row_idx > last_event.row_idx):
                end = max(end, event.end_us)
                last_event = event
            continue
        segments.append(BusySegment(start, end, first_event, last_event))
        start = event.start_us
        end = event.end_us
        first_event = event
        last_event = event
    segments.append(BusySegment(start, end, first_event, last_event))
    return segments


def evidence_event(event: NormalizedEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "event_id": event.event_id,
        "rank_id": event.rank_id,
        "row_idx": event.row_idx,
        "name": event.name_raw,
        "task_type": event.task_type,
        "accelerator_core": event.accelerator_core,
        "stream_id": event.stream_id,
        "start_us": round(event.start_us, 3),
        "duration_us": round(event.duration_us, 3),
        "wait_us": round(event.wait_us, 3),
        "categories": list(event.op_categories),
        "roles": list(event.op_roles),
        "shape_signature": event.shape_signature,
    }


def bubble_windows(events: Sequence[NormalizedEvent], *, limit: int | None = None) -> list[dict[str, Any]]:
    if limit is not None and limit <= 0:
        return []
    segments = merge_event_segments(events)
    if len(segments) < 2:
        return []
    rows: list[dict[str, Any]] = []
    for idx, (left, right) in enumerate(zip(segments[:-1], segments[1:])):
        if right.start_us <= left.end_us:
            continue
        rows.append(
            {
                "bubble_index": idx,
                "start_us": round(left.end_us, 3),
                "end_us": round(right.start_us, 3),
                "duration_us": round(right.start_us - left.end_us, 3),
                "duration_ms": round((right.start_us - left.end_us) / 1000.0, 6),
                "before_event": evidence_event(left.last_event),
                "after_event": evidence_event(right.first_event),
            }
        )
    rows.sort(key=lambda item: float(item["duration_us"]), reverse=True)
    return rows if limit is None else rows[:limit]


def metrics_for_events(events: Sequence[NormalizedEvent], *, top_gap_limit: int = 5) -> dict[str, Any]:
    if not events:
        return {
            "event_count": 0,
            "row_start": None,
            "row_end": None,
            "start_us": None,
            "end_us": None,
            "wall_ms": 0.0,
            "busy_union_ms": 0.0,
            "kernel_sum_ms": 0.0,
            "total_cost_ms": 0.0,
            "wait_sum_ms": 0.0,
            "underfeed_ms": 0.0,
            "underfeed_ratio": 0.0,
            "internal_bubble_total_ms": 0.0,
            "largest_internal_bubble_ms": 0.0,
            "bubble_count": 0,
            "stream_count": 0,
            "task_type_counts": {},
            "role_counts": {},
            "category_counts": {},
            "top_bubbles": [],
        }
    start = min(event.start_us for event in events)
    end = max(event.end_us for event in events)
    wall_us = max(0.0, end - start)
    segments = merge_event_segments(events)
    busy_us = sum(segment.duration_us for segment in segments)
    gaps = [
        right.start_us - left.end_us
        for left, right in zip(segments[:-1], segments[1:])
        if right.start_us > left.end_us
    ]
    kernel_sum_us = sum(event.duration_us for event in events)
    wait_sum_us = sum(event.wait_us for event in events)
    return {
        "event_count": len(events),
        "row_start": min(event.row_idx for event in events),
        "row_end": max(event.row_idx for event in events),
        "start_us": round(start, 3),
        "end_us": round(end, 3),
        "wall_ms": round(wall_us / 1000.0, 6),
        "busy_union_ms": round(busy_us / 1000.0, 6),
        "kernel_sum_ms": round(kernel_sum_us / 1000.0, 6),
        "total_cost_ms": round((kernel_sum_us + wait_sum_us) / 1000.0, 6),
        "wait_sum_ms": round(wait_sum_us / 1000.0, 6),
        "underfeed_ms": round(max(0.0, wall_us - busy_us) / 1000.0, 6),
        "underfeed_ratio": round((max(0.0, wall_us - busy_us) / wall_us) if wall_us > 0 else 0.0, 6),
        "internal_bubble_total_ms": round(sum(gaps) / 1000.0, 6),
        "largest_internal_bubble_ms": round((max(gaps) if gaps else 0.0) / 1000.0, 6),
        "bubble_count": len(gaps),
        "stream_count": len({event.stream_id for event in events}),
        "task_type_counts": dict(sorted(Counter(event.task_type for event in events).items())),
        "role_counts": dict(sorted(Counter(role for event in events for role in event.op_roles).items())),
        "category_counts": dict(sorted(Counter(cat for event in events for cat in event.op_categories).items())),
        "top_bubbles": bubble_windows(events, limit=top_gap_limit) if top_gap_limit > 0 else [],
    }


def select_events(events: Sequence[NormalizedEvent], row_start: int, row_end: int) -> list[NormalizedEvent]:
    left = int(row_start)
    right = int(row_end)
    return [event for event in events if left <= event.row_idx <= right]


def load_events(path: Path) -> list[NormalizedEvent]:
    if path.suffix.lower() == ".csv":
        return load_events_csv(path)
    if not path.exists() and path.with_suffix(".csv").exists():
        return load_events_csv(path.with_suffix(".csv"))
    rows: list[NormalizedEvent] = []
    for item in read_jsonl(path):
        raw_ref = item.get("raw_fields_ref")
        rows.append(
            NormalizedEvent(
                event_id=str(item["event_id"]),
                profile_id=str(item.get("profile_id") or ""),
                rank_id=str(item["rank_id"]),
                source_id=str(item["source_id"]),
                row_idx=int(item["row_idx"]),
                name_raw=str(item.get("name_raw") or ""),
                task_type=str(item.get("task_type") or ""),
                accelerator_core=str(item.get("accelerator_core") or ""),
                stream_id=str(item.get("stream_id") or ""),
                start_us=float(item.get("start_us") or 0.0),
                end_us=float(item.get("end_us") or 0.0),
                duration_us=float(item.get("duration_us") or 0.0),
                wait_us=float(item.get("wait_us") or 0.0),
                op_categories=tuple(item.get("op_categories") or ()),
                op_roles=tuple(item.get("op_roles") or ()),
                shape_signature=item.get("shape_signature"),
                shape_features=dict(item.get("shape_features") or {}),
                pipeline_us=dict(item.get("pipeline_us") or {}),
                op_type=str(item.get("op_type") or "unknown"),
                raw_fields_ref=SourceRef(**raw_ref) if isinstance(raw_ref, dict) else None,
            )
        )
    return sorted(rows, key=lambda event: (event.rank_id, event.row_idx))


def load_events_csv(path: Path) -> list[NormalizedEvent]:
    rows: list[NormalizedEvent] = []
    json_cache: dict[str, Any] = {"[]": [], "{}": {}}
    for _row_number, item in iter_csv_rows(path):
        categories_text = item.get("op_categories") or "[]"
        roles_text = item.get("op_roles") or "[]"
        shape_text = item.get("shape_features") or "{}"
        pipeline_text = item.get("pipeline_us") or "{}"
        categories = json_cache.get(categories_text)
        if categories is None:
            categories = json.loads(categories_text)
            json_cache[categories_text] = categories
        roles = json_cache.get(roles_text)
        if roles is None:
            roles = json.loads(roles_text)
            json_cache[roles_text] = roles
        shape_features = json_cache.get(shape_text)
        if shape_features is None:
            shape_features = json.loads(shape_text)
            json_cache[shape_text] = shape_features
        pipeline_us = json_cache.get(pipeline_text)
        if pipeline_us is None:
            pipeline_us = json.loads(pipeline_text)
            json_cache[pipeline_text] = pipeline_us
        rows.append(
            NormalizedEvent(
                event_id=str(item["event_id"]),
                profile_id=str(item.get("profile_id") or ""),
                rank_id=str(item["rank_id"]),
                source_id=str(item["source_id"]),
                row_idx=int(item["row_idx"]),
                name_raw=str(item.get("name_raw") or ""),
                task_type=str(item.get("task_type") or ""),
                accelerator_core=str(item.get("accelerator_core") or ""),
                stream_id=str(item.get("stream_id") or ""),
                start_us=float(item.get("start_us") or 0.0),
                end_us=float(item.get("end_us") or 0.0),
                duration_us=float(item.get("duration_us") or 0.0),
                wait_us=float(item.get("wait_us") or 0.0),
                op_categories=tuple(categories),
                op_roles=tuple(roles),
                shape_signature=item.get("shape_signature") or None,
                shape_features=dict(shape_features),
                pipeline_us=dict(pipeline_us),
                op_type=str(item.get("op_type") or "unknown"),
                raw_fields_ref=None,
            )
        )
    return rows


def group_by_rank(events: Sequence[NormalizedEvent]) -> dict[str, list[NormalizedEvent]]:
    grouped: dict[str, list[NormalizedEvent]] = {}
    for event in events:
        grouped.setdefault(event.rank_id, []).append(event)
    for rank_events in grouped.values():
        rank_events.sort(key=lambda event: event.row_idx)
    return dict(sorted(grouped.items()))


def load_step_segments(path: Path) -> list[StepSegment]:
    payload = read_json(path, default={})
    rows = payload.get("step_segments", payload if isinstance(payload, list) else [])
    return [
        StepSegment(
            segment_id=str(item["segment_id"]),
            rank_id=str(item["rank_id"]),
            segment_type=str(item["segment_type"]),
            complete=bool(item.get("complete")),
            row_start=int(item["row_start"]),
            row_end=int(item["row_end"]),
            start_us=float(item.get("start_us") or 0.0),
            end_us=float(item.get("end_us") or 0.0),
            cluster_id=item.get("cluster_id"),
            step_family=item.get("step_family"),
            main_layer_count=item.get("main_layer_count"),
            speculative_layer_count=item.get("speculative_layer_count"),
            structure_signature=item.get("structure_signature"),
            layer_ids=tuple(item.get("layer_ids") or ()),
            evidence_ids=tuple(item.get("evidence_ids") or ()),
        )
        for item in rows
    ]


def load_layer_segments(path: Path) -> list[LayerSegment]:
    payload = read_json(path, default={})
    rows = payload.get("layer_segments", payload if isinstance(payload, list) else [])
    return [
        LayerSegment(
            layer_id=str(item["layer_id"]),
            rank_id=str(item["rank_id"]),
            segment_id=str(item["segment_id"]),
            layer_index=int(item["layer_index"]),
            layer_role=str(item.get("layer_role") or "main"),
            boundary_source=str(item.get("boundary_source") or "unknown"),
            row_start=int(item["row_start"]),
            row_end=int(item["row_end"]),
            start_us=float(item.get("start_us") or 0.0),
            end_us=float(item.get("end_us") or 0.0),
            structure_signature=item.get("structure_signature"),
            evidence_ids=tuple(item.get("evidence_ids") or ()),
        )
        for item in rows
    ]


def load_block_segments(path: Path) -> list[BlockSegment]:
    payload = read_json(path, default={})
    rows = payload.get("block_segments", payload if isinstance(payload, list) else [])
    return [
        BlockSegment(
            block_id=str(item["block_id"]),
            rank_id=str(item["rank_id"]),
            segment_id=str(item["segment_id"]),
            layer_id=str(item["layer_id"]),
            layer_index=int(item.get("layer_index") or 0),
            block_index=int(item.get("block_index") or 0),
            block_kind=str(item.get("block_kind") or "other"),
            companion_layer=bool(item.get("companion_layer")),
            row_start=int(item["row_start"]),
            row_end=int(item["row_end"]),
            start_us=float(item.get("start_us") or 0.0),
            end_us=float(item.get("end_us") or 0.0),
            event_count=int(item.get("event_count") or 0),
            evidence_ids=tuple(item.get("evidence_ids") or ()),
        )
        for item in rows
    ]


def write_xlsx(path: Path, sheets: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    """Write a minimal XLSX workbook using only the standard library."""

    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_items = [(safe_sheet_name(name), list(rows)) for name, rows in sheets.items()]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
""" + "".join(
                f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for idx, _ in enumerate(sheet_items, 1)
            ) + "\n</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
""" + "".join(
                f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
                for idx, _ in enumerate(sheet_items, 1)
            ) + f'<Relationship Id="rId{len(sheet_items)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            + "\n</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>
""" + "".join(
                f'<sheet name="{escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
                for idx, (name, _) in enumerate(sheet_items, 1)
            ) + "</sheets></workbook>",
        )
        zf.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>""",
        )
        for idx, (_, rows) in enumerate(sheet_items, 1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml(rows))


def safe_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", name)[:31]
    return cleaned or "Sheet"


def sheet_xml(rows: Sequence[Mapping[str, Any]]) -> str:
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(str(key))
                fieldnames.append(str(key))
    if not fieldnames:
        fieldnames = ["empty"]
    table = [dict(zip(fieldnames, fieldnames))]
    table.extend({key: row.get(key, "") for key in fieldnames} for row in rows)
    xml_rows = []
    for r_idx, row in enumerate(table, 1):
        cells = []
        for c_idx, key in enumerate(fieldnames, 1):
            value = csv_value(row.get(key, ""))
            ref = f"{column_name(c_idx)}{r_idx}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        xml_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )


def column_name(index: int) -> str:
    out = ""
    while index:
        index, remainder = divmod(index - 1, SPREADSHEET_COLUMN_BASE)
        out = chr(65 + remainder) + out
    return out


def csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))
