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
from typing import Any, Mapping

try:
    from .store import pick, row_key_lookup, try_float
except ImportError:  # pragma: no cover - script-mode fallback
    from store import pick, row_key_lookup, try_float  # type: ignore[no-redef]


SHAPE_SIGNATURE_DIM_SAMPLE_LIMIT = 32


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
        lowered = row_key_lookup(row_keys)
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
