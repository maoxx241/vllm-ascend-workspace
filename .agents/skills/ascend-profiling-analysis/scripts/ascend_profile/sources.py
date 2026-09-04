#!/usr/bin/env python3
"""Profiling-root discovery and kernel_details.csv row extraction.

Everything the normalize stage needs to locate rank directories and pull
canonical fields (name / task type / accelerator core / stream / timing /
shape signature) out of raw CANN ``kernel_details.csv`` rows.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .pipeline import _PIPELINE_SOURCE_COLUMNS
    from .store import pick, pick_at, pick_resolved, resolve_pick_keys, resolve_pick_positions, try_float
    from .work import (
        INPUT_DTYPE_CANDIDATES,
        INPUT_SHAPE_CANDIDATES,
        OUTPUT_DTYPE_CANDIDATES,
        OUTPUT_SHAPE_CANDIDATES,
    )
except ImportError:  # pragma: no cover - script-mode fallback
    from pipeline import _PIPELINE_SOURCE_COLUMNS  # type: ignore[no-redef]
    from store import pick, pick_at, pick_resolved, resolve_pick_keys, resolve_pick_positions, try_float  # type: ignore[no-redef]
    from work import (  # type: ignore[no-redef]
        INPUT_DTYPE_CANDIDATES,
        INPUT_SHAPE_CANDIDATES,
        OUTPUT_DTYPE_CANDIDATES,
        OUTPUT_SHAPE_CANDIDATES,
    )


SHAPE_SIGNATURE_DIM_SAMPLE_LIMIT = 32

# Candidate column aliases for the canonical kernel_details.csv fields.
# Resolution semantics follow ``store.pick``: case-insensitive match, first
# candidate whose column exists and carries a non-empty stripped value wins.
# These lists are the single source of truth shared by the per-row
# ``*_from_row`` helpers and the per-file ``KernelRowAccessor`` below.
NAME_CANDIDATES = ("Name", "Op Name", "Kernel Name", "Operation Name")
TASK_TYPE_CANDIDATES = ("Task Type", "Kernel Type", "Type")
CORE_CANDIDATES = ("Accelerator Core", "Core Type", "Task Type", "Kernel Type", "Type")
STREAM_CANDIDATES = ("Stream ID", "StreamId", "Stream", "stream_id")
START_TIME_CANDIDATES = ("Start Time(us)", "Start Time", "Start(us)", "Start", "ts")
DURATION_CANDIDATES = ("Duration(us)", "Duration", "dur")
WAIT_CANDIDATES = ("Wait Time(us)", "Wait Time", "Wait(us)", "wait")
END_TIME_CANDIDATES = ("End Time(us)", "End Time", "End(us)", "End")
SHAPE_SIGNATURE_CANDIDATES = ("Input Shapes", "Input Shape", "Input", "Output Shapes", "Output Shape", "Output")


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
    if root.is_file() and (root.name == "kernel_details.csv" or _is_kernel_db_name(root.name)):
        return [root.parent.parent if root.parent.name == "ASCEND_PROFILER_OUTPUT" else root.parent]
    if (
        (root / "kernel_details.csv").is_file()
        or (root / "ASCEND_PROFILER_OUTPUT" / "kernel_details.csv").is_file()
        or any(root.glob("ascend_pytorch_profiler_*.db"))
        or any((root / "ASCEND_PROFILER_OUTPUT").glob("ascend_pytorch_profiler_*.db"))
    ):
        return [root]
    candidates: set[Path] = set()
    for pattern in ("kernel_details.csv", "ascend_pytorch_profiler_*.db"):
        for path in root.rglob(pattern):
            parent = path.parent.parent if path.parent.name == "ASCEND_PROFILER_OUTPUT" else path.parent
            candidates.add(parent)
    return sorted(candidates, key=lambda item: str(item))


def _is_kernel_db_name(name: str) -> bool:
    return name.startswith("ascend_pytorch_profiler_") and name.endswith(".db")


def kernel_db_path(rank_dir: Path) -> Path | None:
    """Locate the torch_npu profiler sqlite db for a rank directory."""

    for base in (rank_dir, rank_dir / "ASCEND_PROFILER_OUTPUT"):
        matches = sorted(base.glob("ascend_pytorch_profiler_*.db"))
        if matches:
            return matches[0]
    matches = sorted(rank_dir.glob("**/ascend_pytorch_profiler_*.db"))
    return matches[0] if matches else None


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


_SHAPE_TEXT_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}


def shape_signature_from_text(shape_text: str) -> tuple[str | None, dict[str, Any]]:
    """Pure ``shape_signature`` core over the joined shape cell text.

    Identical shape cells recur across rows of the same kernel, so the
    parse + digest is memoized on the text itself.
    """

    if not shape_text:
        return None, {}
    cached = _SHAPE_TEXT_CACHE.get(shape_text)
    if cached is not None:
        return cached
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
    result = (f"shape_{digest}", features)
    _SHAPE_TEXT_CACHE[shape_text] = result
    return result


def shape_signature(row: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    row_keys = tuple(str(key) for key in row.keys())
    shape_columns = _SHAPE_COLUMN_CACHE.get(row_keys)
    if shape_columns is None:
        shape_columns = resolve_pick_keys(row_keys, SHAPE_SIGNATURE_CANDIDATES)
        _SHAPE_COLUMN_CACHE[row_keys] = shape_columns
    if not shape_columns:
        return None, {}
    shape_text = " ".join(str(row.get(key, "")).strip() for key in shape_columns).strip()
    return shape_signature_from_text(shape_text)


def task_type_from_row(row: Mapping[str, Any]) -> str:
    return pick(row, TASK_TYPE_CANDIDATES, "UNKNOWN").upper()


def core_from_row(row: Mapping[str, Any]) -> str:
    return pick(row, CORE_CANDIDATES, "UNKNOWN").upper()


def name_from_row(row: Mapping[str, Any]) -> str:
    return pick(row, NAME_CANDIDATES, "UNKNOWN")


def stream_from_row(row: Mapping[str, Any]) -> str:
    return pick(row, STREAM_CANDIDATES, "unknown")


def event_time_from_row(row: Mapping[str, Any]) -> tuple[float, float, float, float]:
    start = try_float(pick(row, START_TIME_CANDIDATES, "0"))
    duration = try_float(pick(row, DURATION_CANDIDATES, "0"))
    wait = try_float(pick(row, WAIT_CANDIDATES, "0"))
    end = try_float(pick(row, END_TIME_CANDIDATES, "0"))
    if end <= start:
        end = start + max(0.0, duration)
    if duration <= 0 and end > start:
        duration = end - start
    return start, end, max(0.0, duration), max(0.0, wait)


class KernelRowAccessor:
    """Per-file pre-resolved positional accessor for kernel_details.csv rows.

    Normalize makes ~23 ``store.pick`` calls per row and each call re-folds
    every candidate alias against the row's key set; on multi-million-row
    captures that case-folding dominates the whole stage.  All rows of one
    file share the header, so this accessor resolves each logical field to
    its column position(s) once (via ``store.resolve_pick_positions``) and
    then indexes ``csv.reader`` rows directly.  Resolution semantics are
    exactly ``pick``'s: case-insensitive alias match, candidate order
    preserved, first non-empty stripped value wins, same defaults as the
    ``*_from_row`` helpers above.  Short rows reproduce the
    ``csv.DictReader`` + ``pick`` behaviour (missing trailing cells
    stringify to ``"None"`` — see ``store.pick_at``).
    """

    def __init__(self, header: Sequence[str]) -> None:
        header = tuple(str(key) for key in header)
        self._name_pos = resolve_pick_positions(header, NAME_CANDIDATES)
        self._task_pos = resolve_pick_positions(header, TASK_TYPE_CANDIDATES)
        self._core_pos = resolve_pick_positions(header, CORE_CANDIDATES)
        self._stream_pos = resolve_pick_positions(header, STREAM_CANDIDATES)
        self._start_pos = resolve_pick_positions(header, START_TIME_CANDIDATES)
        self._duration_pos = resolve_pick_positions(header, DURATION_CANDIDATES)
        self._wait_pos = resolve_pick_positions(header, WAIT_CANDIDATES)
        self._end_pos = resolve_pick_positions(header, END_TIME_CANDIDATES)
        self._shape_pos = resolve_pick_positions(header, SHAPE_SIGNATURE_CANDIDATES)
        self._pipeline_pos = tuple(
            resolve_pick_positions(header, candidates)
            for _field, candidates in _PIPELINE_SOURCE_COLUMNS
        )
        self._input_shape_pos = resolve_pick_positions(header, INPUT_SHAPE_CANDIDATES)
        self._output_shape_pos = resolve_pick_positions(header, OUTPUT_SHAPE_CANDIDATES)
        self._input_dtype_pos = resolve_pick_positions(header, INPUT_DTYPE_CANDIDATES)
        self._output_dtype_pos = resolve_pick_positions(header, OUTPUT_DTYPE_CANDIDATES)
        # text -> parsed pipeline value; identical cell strings (zero rows,
        # repeated kernels) recur constantly within one file.
        self._num_cache: dict[str, float] = {}

    def name(self, row: Sequence[str]) -> str:
        return pick_at(row, self._name_pos, "UNKNOWN")

    def task_type(self, row: Sequence[str]) -> str:
        return pick_at(row, self._task_pos, "UNKNOWN").upper()

    def core(self, row: Sequence[str]) -> str:
        return pick_at(row, self._core_pos, "UNKNOWN").upper()

    def stream(self, row: Sequence[str]) -> str:
        return pick_at(row, self._stream_pos, "unknown")

    def event_time(self, row: Sequence[str]) -> tuple[float, float, float, float]:
        start = try_float(pick_at(row, self._start_pos, "0"))
        duration = try_float(pick_at(row, self._duration_pos, "0"))
        wait = try_float(pick_at(row, self._wait_pos, "0"))
        end = try_float(pick_at(row, self._end_pos, "0"))
        if end <= start:
            end = start + max(0.0, duration)
        if duration <= 0 and end > start:
            duration = end - start
        return start, end, max(0.0, duration), max(0.0, wait)

    def pipeline_breakdown(self, row: Sequence[str]) -> dict[str, float]:
        """Same output as ``pipeline.pipeline_breakdown_from_row``."""
        out: dict[str, float] = {}
        num_cache = self._num_cache
        row_len = len(row)
        for (field, _candidates), positions in zip(_PIPELINE_SOURCE_COLUMNS, self._pipeline_pos):
            text = ""
            for pos in positions:
                # inline store.pick_at: 15 fields per row make the call
                # overhead measurable on multi-million-row captures.
                value = row[pos] if pos < row_len else None
                candidate = str(value).strip()
                if candidate:
                    text = candidate
                    break
            if not text:
                continue
            value_f = num_cache.get(text)
            if value_f is None:
                value_f = round(max(0.0, try_float(text)), 6)
                num_cache[text] = value_f
            out[field] = value_f
        return out

    def work_fields(self, row: Sequence[str]) -> tuple[str, str, str, str]:
        """Raw ``(input_shapes, output_shapes, input_dtypes, output_dtypes)``
        cells, exactly as ``work.estimated_work_from_row`` would pick them.

        These raw strings (plus name/task_type/op_type) fully determine the
        work estimate, so callers can memoize on them without parsing.
        """
        return (
            pick_at(row, self._input_shape_pos, ""),
            pick_at(row, self._output_shape_pos, ""),
            pick_at(row, self._input_dtype_pos, ""),
            pick_at(row, self._output_dtype_pos, ""),
        )

    def shape_text(self, row: Sequence[str]) -> str:
        """Joined shape cells, exactly as ``shape_signature`` builds them."""
        row_len = len(row)
        return " ".join(
            str(row[pos] if pos < row_len else None).strip()
            for pos in self._shape_pos
        ).strip()
