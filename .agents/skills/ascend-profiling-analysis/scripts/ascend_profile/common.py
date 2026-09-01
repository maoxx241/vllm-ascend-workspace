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


_NULLISH_TEXT = {"", "N/A", "NA", "NAN", "NONE", "NULL"}
_DTYPE_BYTES: dict[str, float] = {
    "BOOL": 1.0,
    "UINT8": 1.0,
    "INT8": 1.0,
    "S8": 1.0,
    "FP8": 1.0,
    "FLOAT8": 1.0,
    # dtype_bytes() strips underscores before lookup, so the keys must be the
    # normalized spellings (the FP8/FLOAT8 keys above are the fallback).
    "FLOAT8E4M3FN": 1.0,
    "FLOAT8E5M2": 1.0,
    "UINT16": 2.0,
    "INT16": 2.0,
    "FP16": 2.0,
    "FLOAT16": 2.0,
    "HALF": 2.0,
    "BF16": 2.0,
    "BFLOAT16": 2.0,
    "UINT32": 4.0,
    "INT32": 4.0,
    "FLOAT": 4.0,
    "FP32": 4.0,
    "FLOAT32": 4.0,
    "UINT64": 8.0,
    "INT64": 8.0,
    "DOUBLE": 8.0,
    "FLOAT64": 8.0,
    "FP4": 0.5,
    "INT4": 0.5,
    "UINT4": 0.5,
}

_MATMUL_TASK_HINTS = (
    "MATMUL",
    "GEMM",
    "BATCHMATMUL",
    "GROUPEDMATMUL",
    "GROUPED_MATMUL",
)
_ATTENTION_TASK_HINTS = (
    "FLASHATTENTION",
    "FLASH_ATTENTION",
    "INCREFLASHATTENTION",
    "PROMPTFLASHATTENTION",
    "PAGEDATTENTION",
    "SPARSEATTN",
    "SPARSEATTENTION",
    "ATTENTION",
)
_VECTOR_FLOPS_PER_ELEM: dict[str, float] = {
    "ADD": 1.0,
    "SUB": 1.0,
    "MUL": 1.0,
    "DIV": 1.0,
    "REALDIV": 1.0,
    "NEG": 1.0,
    "SQRT": 1.0,
    "RSQRT": 1.0,
    "EXP": 2.0,
    "LOG": 2.0,
    "SIGMOID": 4.0,
    "TANH": 4.0,
    "GELU": 8.0,
    "FASTGELU": 8.0,
    "SILU": 4.0,
    "SWIGLU": 8.0,
    "SOFTMAX": 5.0,
    "RMSNORM": 5.0,
    "LAYERNORM": 6.0,
    "REDUCESUM": 1.0,
    "REDUCEMEAN": 1.0,
    "CAST": 0.0,
    "COPY": 0.0,
    "MEMCPY": 0.0,
    "RESHAPE": 0.0,
    "TRANSPOSE": 0.0,
    "SLICE": 0.0,
    "STRIDEDSLICE": 0.0,
    "CONCAT": 0.0,
    "GATHER": 0.0,
    "SCATTER": 0.0,
    "ZEROSLIKE": 0.0,
    "FILL": 0.0,
}


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


def emit_stage_json(payload: dict[str, Any]) -> None:
    """Emit a stage CLI's summary JSON to stdout, terminated by a newline.

    Callers (analyze/segment/classify/summarize/cross_rank/diagnostics/report)
    should funnel their final printout through this helper so wrappers and
    automation can consume valid JSON instead of Python dict repr.
    """
    import sys as _sys
    _sys.stdout.write(json.dumps(to_plain(payload), ensure_ascii=False) + "\n")
    _sys.stdout.flush()


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


def dtype_bytes(dtype: Any, default: float = 2.0) -> float:
    """Return storage bytes for a CANN/HF dtype token.

    Used only for derived operator/model estimates. Missing or unknown
    dtypes fall back to BF16-size bytes so the report can still rank
    obvious matmul / attention hotspots while marking the figures as
    estimates.
    """

    token = re.sub(r"[^A-Za-z0-9]+", "", str(dtype or "")).upper()
    if not token:
        return default
    if token in _DTYPE_BYTES:
        return _DTYPE_BYTES[token]
    for key, size in _DTYPE_BYTES.items():
        if key in token:
            return size
    return default


def parse_tensor_shapes(raw: Any) -> list[list[int]]:
    """Parse CANN shape cells such as ``"1,32,1,128;1,2,4096,128"``.

    The profiler has emitted several textual variants across CANN
    versions.  We keep this parser intentionally permissive and
    evidence-safe: malformed tensor fragments are skipped instead of
    fabricated.
    """

    if raw is None:
        return []
    text = str(raw).strip().strip('"').strip()
    if text.upper() in _NULLISH_TEXT:
        return []
    tensors: list[list[int]] = []
    for part in text.split(";"):
        fragment = part.strip().strip('"').strip()
        if not fragment or fragment.upper() in _NULLISH_TEXT:
            continue
        dims = [int(float(value)) for value in re.findall(r"-?\d+(?:\.\d+)?", fragment)]
        dims = [dim for dim in dims if dim > 0]
        if dims:
            tensors.append(dims)
    return tensors


def parse_tensor_dtypes(raw: Any) -> list[str]:
    if raw is None:
        return []
    text = str(raw).strip().strip('"').strip()
    if text.upper() in _NULLISH_TEXT:
        return []
    sep = ";" if ";" in text else ","
    return [item.strip().strip('"').strip().upper() for item in text.split(sep) if item.strip()]


def tensor_numel(shape: Sequence[int]) -> int:
    total = 1
    for dim in shape:
        total *= int(dim)
    return total if shape else 0


def tensor_list_bytes(shapes: Sequence[Sequence[int]], dtypes: Sequence[str]) -> float:
    total = 0.0
    for idx, shape in enumerate(shapes):
        dtype = dtypes[idx] if idx < len(dtypes) else (dtypes[-1] if dtypes else "")
        total += tensor_numel(shape) * dtype_bytes(dtype)
    return total


def _matrix_dims(shape: Sequence[int]) -> tuple[int, int, int]:
    """Return ``(M, K, batch)`` for a rank-N matrix tensor."""

    if len(shape) < 2:
        return 0, 0, 0
    batch = 1
    for dim in shape[:-2]:
        batch *= int(dim)
    return int(shape[-2]), int(shape[-1]), batch


def estimate_matmul_flops(shapes: Sequence[Sequence[int]]) -> float | None:
    mats = [list(shape) for shape in shapes if len(shape) >= 2]
    if len(mats) < 2:
        return None
    m, k, batch = _matrix_dims(mats[0])
    b_rows, b_cols, _b_batch = _matrix_dims(mats[1])
    if m <= 0 or k <= 0 or min(b_rows, b_cols) <= 0:
        return None
    n = b_cols if b_rows == k else b_rows if b_cols == k else b_cols
    if n <= 0:
        return None
    # For GroupedMatmul the first operand's token dimension already
    # totals the visited experts; multiplying by the expert dimension of
    # the weight tensor would over-count.
    return float(2 * m * n * k * max(batch, 1))


def estimate_attention_flops(
    input_shapes: Sequence[Sequence[int]],
    output_shapes: Sequence[Sequence[int]],
) -> float | None:
    """Best-effort fused-attention FLOP estimate from CANN shape cells.

    We use the first query-like 4D tensor and the next key-like 4D tensor
    as ``[B, H, S, D]`` / ``[B, Hkv, T, D]`` style layouts.  The result
    is only used as a ranking lens, not as an evidence-chain conclusion.
    """

    shapes = [list(shape) for shape in input_shapes if len(shape) >= 3]
    if not shapes:
        return None
    q = next((shape for shape in shapes if len(shape) >= 4), shapes[0])
    if len(q) >= 4:
        batch = q[0]
        heads = q[1]
        seq_q = q[-2]
        head_dim = q[-1]
    else:
        batch = 1
        heads = q[0]
        seq_q = q[-2]
        head_dim = q[-1]
    k_shape = next(
        (
            shape
            for shape in shapes[1:]
            if len(shape) >= 3 and shape[-1] == head_dim
        ),
        None,
    )
    seq_k = k_shape[-2] if k_shape is not None else seq_q
    if min(batch, heads, seq_q, seq_k, head_dim) <= 0:
        return None
    # QK^T + PV.  For prefill causal masks this is an upper bound; the
    # report labels it as an estimate.
    return float(4 * batch * heads * seq_q * seq_k * head_dim)


def estimate_vector_flops(task_type: str, name: str, shapes: Sequence[Sequence[int]]) -> float | None:
    token = fold_text(f"{task_type} {name}").upper()
    factor: float | None = None
    # Match longer (more specific) keys first: "ADDRMSNORM" contains both
    # "ADD" and "RMSNORM", and insertion order must not decide the outcome.
    for key in sorted(_VECTOR_FLOPS_PER_ELEM, key=len, reverse=True):
        if key in token:
            factor = _VECTOR_FLOPS_PER_ELEM[key]
            break
    if factor is None:
        factor = 1.0
    if factor <= 0:
        return None
    elems = max((tensor_numel(shape) for shape in shapes), default=0)
    return float(elems * factor) if elems > 0 else None


def estimated_work_from_row(row: Mapping[str, Any], *, name: str, task_type: str, op_type: str) -> dict[str, Any]:
    """Estimate per-event bytes/FLOPs from raw CANN shape and dtype fields.

    This adapts the useful part of LLMInsight's operator calculation
    model into the profiling pipeline.  It is deliberately conservative:
    unknown kernels keep byte counts when shapes exist, but FLOPs stay
    unset unless a known matmul/attention/vector pattern matches.
    """

    input_shapes = parse_tensor_shapes(pick(row, ("Input Shapes", "Input Shape", "Input"), ""))
    output_shapes = parse_tensor_shapes(pick(row, ("Output Shapes", "Output Shape", "Output"), ""))
    input_dtypes = parse_tensor_dtypes(pick(row, ("Input Data Types", "Input Data Type", "Input Dtypes"), ""))
    output_dtypes = parse_tensor_dtypes(pick(row, ("Output Data Types", "Output Data Type", "Output Dtypes"), ""))
    bytes_est = tensor_list_bytes(input_shapes, input_dtypes) + tensor_list_bytes(output_shapes, output_dtypes)
    text = f"{task_type} {name}".upper()
    all_shapes = [*input_shapes, *output_shapes]
    work_class = "unknown"
    flops: float | None = None
    if any(hint in text for hint in _MATMUL_TASK_HINTS):
        work_class = "matmul"
        flops = estimate_matmul_flops(input_shapes)
    elif any(hint in text for hint in _ATTENTION_TASK_HINTS):
        work_class = "attention"
        flops = estimate_attention_flops(input_shapes, output_shapes)
    elif op_type in {"aiv", "mix_comm_aiv", "mix_cv"}:
        work_class = "vector"
        flops = estimate_vector_flops(task_type, name, all_shapes)
    elif bytes_est > 0:
        work_class = "memory"

    dtype = input_dtypes[0] if input_dtypes else (output_dtypes[0] if output_dtypes else "")
    out: dict[str, Any] = {
        "estimated_bytes": round(bytes_est, 3),
        "estimated_work_class": work_class,
        "estimated_dtype": dtype,
    }
    if flops is not None:
        out["estimated_flops"] = round(flops, 3)
        if bytes_est > 0:
            out["estimated_arithmetic_intensity"] = round(flops / bytes_est, 6)
    # Store enough shape evidence for debugging without bloating every row
    # with the full raw shape cell.
    if input_shapes:
        out["input_shape_count"] = len(input_shapes)
        out["input_shape_sample"] = input_shapes[:3]
    if output_shapes:
        out["output_shape_count"] = len(output_shapes)
        out["output_shape_sample"] = output_shapes[:3]
    return out


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
    """Classify one kernel into op_categories + op_roles.

    The rule order and signatures below are the Python mirror of
    ``knowledge/kernel_signatures.yaml``.  When adding a new kernel:

    1. Add it to ``kernel_signatures.yaml`` with ``evidence: path:line``.
    2. If it changes a family's must-have set, update
       ``attention_families.yaml`` or ``moe_families.yaml``.
    3. Mirror the rule here.
    4. Add any new category to ``semantic_conventions.yaml`` so the
       schema test passes.

    **Naming policy — paper vs CANN backend:**

    * Architecture-family labels (``mla`` / ``dsa`` / ``csa`` / ``hca``)
      are the names used in the DeepSeek papers and are what we surface
      in the report.
    * CANN / vllm-ascend route them through *backend* classes:
      ``AscendMLAImpl`` for MLA, ``AscendSFAImpl`` for both DSA (V3.2)
      and CSA (V4). The runtime backend is annotated separately and is
      NOT used as a category name to avoid hiding the paper-level
      distinction.
    * Kernel-level categories below are **neutral** so the same Compressor
      kernel can serve both CSA (V4) and HCA (V4); the architecture
      family is then resolved from the *combination* of kernels present
      in a block.
    """
    cache_key = (name, task_type, accelerator_core)
    cached = _CATEGORY_ROLE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    text = fold_text(f"{name} {task_type} {accelerator_core}")
    categories: set[str] = set()
    roles: set[str] = set()

    # --- Communication ----------------------------------------------------
    if any(token in text for token in ("hccl", "hcom", "allreduce", "allgather", "reducescatter", "alltoall")):
        categories.add("communication.collective")
        roles.add("communication")
        if "allreduce" in text:
            categories.add("communication.allreduce")
        if "allgather" in text:
            categories.add("communication.allgather")
        if "reducescatter" in text:
            categories.add("communication.reducescatter")
        if "alltoall" in text:
            categories.add("communication.alltoallv")

    # --- Attention: sparse-attention building blocks ----------------------
    #     These appear in BOTH DSA (V3.2) and CSA (V4). The architecture
    #     family is decided later from the *combination* (see
    #     html_report.detect_attention_subtype):
    #       - kv_compressor + lightning_indexer + sparse_sharedkv  -> CSA (V4)
    #       - lightning_indexer + sparse_sharedkv, no kv_compressor -> DSA (V3.2)
    #       - kv_compressor + dense FIA, no indexer/sparse_sharedkv -> HCA (V4)
    if any(token in text for token in ("sparseattnsharedkv", "sparseattentionsharedkv", "sharedkv")):
        if "metadata" in text:
            categories.add("attention.sparse_sharedkv.metadata")
            roles.add("attention_aux")
        else:
            categories.add("attention.sparse_sharedkv")
            roles.add("attention")
    if any(token in text for token in ("lightningindex", "lightningindexer", "indexercompressepilog")):
        categories.add("attention.lightning_indexer")
        roles.add("attention_aux")
    if "compressor" in text or "kvcompressepilog" in text:
        # KV compression (only V4 CSA / HCA, NOT V3.2 DSA).
        categories.add("attention.kv_compressor")
        roles.add("attention_aux")
    if "batchmatmultranspose" in text:
        # SFA backend custom V up-proj op (sfa_v1.py:841). Used by both
        # DSA and CSA when the ≤1024-token path is selected.
        categories.add("attention.sparse_attn.v_up_proj")
        categories.add("compute.matmul")
        roles.add("attention")

    # --- Attention: MLA (DeepSeek V2 / V3; also reused by DSA in V3.2) ----
    # CANN canonical op names (per the CANN doc and PR #3226 in
    # vllm-ascend): ``mla_prolog``, ``mla_prolog_v2``, plus the
    # vllm-ascend custom kernel ``mla_preprocess``. We accept all three
    # spellings — older traces show "MlaProlog", newer ones "MlaPreprocess".
    if any(token in text for token in (
        "mlapreprocess",
        "mlaprolog",
        "mlaprologv2",
        "mlaprologweightnz",
        "mlapo",
    )):
        categories.add("attention.mla.preprocess")
        categories.add("attention.mla")
        roles.add("attention")
    if "kvrmsnormropecache" in text:
        categories.add("attention.mla.kv_norm_rope_cache")
        categories.add("attention.rope")
        roles.add("attention_aux")
    # Triton / Ascend-C MLA preprocessing variants observed in real
    # traces (DSV2-Lite W8A8 path, GLM4.7). These kernels fuse the
    # qkv split + rmsnorm + rope into a single custom op and serve
    # the same role as KvRmsNormRopeCache on the MLA pipeline.
    # Mirror of ``splitqkvrmsnormrope`` in kernel_signatures.yaml.
    #
    # NOTE: ``fused_qkvzba_split_reshape_cat_kernel`` (folded
    # ``fusedqkvzbasplitreshape``) used to live here but it is
    # NOT an MLA kernel — the ``zba`` suffix is GDN's gate (Z),
    # beta (B) and alpha (A) parameters. It is handled below
    # under the linear/mamba block instead.
    if "splitqkvrmsnormrope" in text:
        categories.add("attention.mla.kv_norm_rope_cache")
        categories.add("attention.rope")
        roles.add("attention_aux")
    if "transposebatchmatmul" in text or "transposequantbatchmatmul" in text:
        # MLA V up-proj uses npu_transpose_batchmatmul (mla_v1.py:893).
        categories.add("attention.mla.v_up_proj")
        categories.add("compute.matmul")
        roles.add("attention")

    # --- Attention: KVComp overlay (Hamming-distance KV pruning) ----------
    if "hammingdisttopk" in text:
        categories.add("attention.kvcomp.topk")
        categories.add("attention.kvcomp")
        roles.add("attention_aux")
    if "signbitspack" in text:
        categories.add("attention.kvcomp.signpack")
        roles.add("attention_aux")
    if "reshapeandcachebnsd" in text:
        categories.add("attention.kvcomp.cache_write")
        roles.add("attention_aux")

    # --- Attention: plain paged-KV cache I/O (dense / ATB path; NOT the
    #     KVComp overlay above). MUST run after the bnsd variant so the
    #     KVComp cache_write keeps its specific category; only the
    #     non-bnsd variants land here. ``fold_text`` strips underscores
    #     so "reshape_and_cache_200000000" reduces to "reshapeandcache..."
    #     and falls through the same "reshapeandcache" substring rule.
    if "attention.kvcomp.cache_write" not in categories and any(token in text for token in (
        "pagedcacheload",
        "scatterpakvcache",
        "reshapeandcache",
    )):
        categories.add("attention.kv_cache_io")
        roles.add("attention_aux")

    # --- Attention: dense GQA / MHA ---------------------------------------
    # FIA / UnpadFA / ATB PagedAttentionMask all serve the same role —
    # they are dense flash-style score kernels. The ATB variant uses a
    # different name (``PagedAttentionMaskNdKernel``) but feeds the
    # same paged KV cache and supports the same MHA/GQA distinction.
    if any(token in text for token in (
        "fusedinferattentionscore",
        "unpadflashattention",
        "flashattentionscore",
        "flashattention",
        "pagedattentionmask",
    )):
        # ``FusedInferAttentionScore`` (FIA) and ``UnpadFlashAttention``
        # are general-purpose flash-style score kernels — per the CANN
        # docs (aclnnFusedInferAttentionScore* / torch_npu
        # .npu_fused_infer_attention_score), they support **MHA, GQA,
        # AND MLA** depending on the ``num_key_value_heads`` parameter:
        #   * num_kv == num_q              → MHA
        #   * 1 < num_kv < num_q           → GQA
        #   * num_kv == 1 (or per latent)  → MLA / MQA-style
        # The kernel category therefore stays neutral
        # (``attention.flash_score``); the architecture family is
        # decided by the *combination* of category signatures present
        # in a block (see resolve_attention_family).
        categories.add("attention.flash_score")
        roles.add("attention")

    # --- Attention: linear / mamba / GDN ----------------------------------
    # Tokens cover the full GDN/Mamba/DeltaNet kernel family observed
    # on Ascend traces:
    #   * ``causalconv`` / ``causalconv1d`` — Mamba/GDN causal 1D conv
    #   * ``mamba`` / ``deltanet`` / ``gdn`` — Mamba2, DeltaNet, Gated DeltaNet hints
    #   * ``recurrentgateddelta`` / ``recurrentdelta`` — GDN's recurrent rule kernel
    #     (e.g. ``RecurrentGatedDeltaRule_*`` from Qwen3-Next).
    #   * ``qkvzbasplit`` (folded form of ``qkvzba_split``) — Qwen3-Next
    #     GDN QKV + Z (gate) + B (beta) + A (alpha) projection split.
    #     Used to be mis-tagged as ``attention.mla.kv_norm_rope_cache``
    #     because the bare ``fusedqkvzbasplitreshape`` token resembled
    #     the MLA ``splitqkvrmsnormrope`` companion; the ``zba`` marker
    #     is unique to GDN and must drive the linear family instead.
    if any(token in text for token in (
        "causalconv",
        "causalconv1d",
        "mamba",
        "deltanet",
        "gdn",
        "recurrentgateddelta",
        "recurrentdelta",
        "qkvzbasplit",
    )):
        categories.add("attention.linear_or_mamba")
        roles.add("attention")

    # --- Attention: RoPE companions ---------------------------------------
    if "interleaverope" in text:
        categories.add("attention.rope.interleave")
        categories.add("attention.rope")
        roles.add("attention_aux")
    if "rotarymul" in text or "partialrotarymul" in text:
        # InPlacePartialRotaryMul / RotaryMul -> npu_rotary_mul.
        categories.add("attention.rope.partial")
        categories.add("attention.rope")
        roles.add("attention_aux")
    if "singlerope" in text:
        # MLA decode single-token rope (mla_v1.py rope_single).
        # Equivalent companion role to InplacePartialRotaryMul.
        categories.add("attention.rope.partial")
        categories.add("attention.rope")
        roles.add("attention_aux")
    if "rotaryembedding" in text and "interleaverope" not in text:
        categories.add("attention.rope.indexed")
        categories.add("attention.rope")
        roles.add("attention_aux")
    # ATB / Triton rope fallback: catches RopeKernel, AtbRopeKernel,
    # RopeWithSinCosCache_*, RotaryPosEmbInfer_*, RotaryPositionEmbedding,
    # rotary_pos_emb_*, _triton_rope, and similar variants. Runs AFTER
    # the specific rules above so RotaryMul / InterleaveRope /
    # SingleRope / NpuRotaryEmbedding keep their refined subtypes; only
    # kernels that still have NO ``attention.rope`` get the umbrella
    # tag (no sub-kind inferred).
    if (
        ("rope" in text or "rotary" in text)
        and "attention.rope" not in categories
        # exclude the MLA preprocessing kernel that already received
        # attention.mla.kv_norm_rope_cache + attention.rope above
        and "kvrmsnormropecache" not in text
        and "splitqkvrmsnormrope" not in text
        and "fusedqkvzbasplitreshape" not in text
    ):
        categories.add("attention.rope")
        roles.add("attention_aux")

    # --- Attention: residual generic catch ---  do NOT tag aux/companion
    # rope/normalization kernels as attention.generic just because they
    # contain "attention" — only tag when we have no specific subtype.
    if "attention" in text and not any(
        cat.startswith("attention.") for cat in categories
    ):
        categories.add("attention.generic")
        roles.add("attention")

    # --- MoE: gating top-k ------------------------------------------------
    if any(token in text for token in ("moegating", "gatingtopk", "topkgating", "topkrouter")):
        categories.add("moe.gating")
        roles.add("moe")
    # NOTE on HC* / MHC prefix: kernels such as ``HCPreSinkhorn``,
    # ``HCPreInvRMS``, ``HCPost``, ``MhcRmsNorm`` carry an ``hc`` / ``mhc``
    # prefix that is NOT HCCL. These appear as structural block-head
    # helpers in BOTH the attention prologue and the MoE routing prologue
    # (verified from a real DSV4 prefill profile where MHC variants show
    # up before attention layers as well). They MUST stay under
    # ``block_head.mhc_prefix`` — putting them in ``moe.gating`` was an
    # earlier mistake; see the block-head heuristic block below.

    # --- MoE: dispatch / combine / fused MC2 ------------------------------
    is_fused_mc2_kernel = any(
        token in text for token in ("dispatchffncombine", "ffncombine", "dispatchgmmcombine")
    )
    if is_fused_mc2_kernel:
        categories.add("moe.dispatch_expert_compute")
        roles.add("moe")
    if "moeinitrouting" in text:
        categories.add("moe.dispatch")
        roles.add("moe")
    if any(token in text for token in ("moedispatch", "dispatch")) and not is_fused_mc2_kernel:
        categories.add("moe.dispatch")
        roles.add("moe")
    if "combine" in text and not is_fused_mc2_kernel:
        categories.add("moe.combine")
        roles.add("moe")

    # --- MoE: expert matmul -----------------------------------------------
    # Beware substring collisions: a fused MC2 kernel name like
    # ``DispatchGmmCombineDecode`` contains ``gmm`` but is a single fused
    # dispatch+expert+combine op, NOT a standalone expert matmul. Guard
    # the broad ``gmm`` rule with the fused-MC2 detection above so the
    # category stays exactly ``moe.dispatch_expert_compute`` for fused
    # MC2 kernels (matches the kernel_signatures.yaml contract).
    if (
        any(token in text for token in ("groupedmatmul", "gmm"))
        and not is_fused_mc2_kernel
    ):
        categories.add("moe.expert_matmul")
        categories.add("compute.matmul")
        roles.add("moe")
        roles.add("compute")
    if "groupedmatmulswigluquant" in text:
        categories.add("moe.expert_matmul")
        categories.add("compute.matmul")
        roles.add("moe")
        roles.add("compute")

    # --- Compute: matmul / BMM (non-MoE / non-MLA-V-up-proj) --------------
    if any(token in text for token in ("batchmatmul", "quantbatchmatmul", "matmul", "gemm")):
        categories.add("compute.matmul")
        roles.add("compute")

    # --- Quantisation -----------------------------------------------------
    if "dynamicmxquant" in text:
        categories.add("quant.mx")
        categories.add("compute.aux")
        roles.add("quant")
    elif "dynamicquant" in text:
        categories.add("quant.dynamic")
        categories.add("compute.aux")
        roles.add("quant")
    if "quantbatchmatmul" in text:
        categories.add("quant.matmul")

    # --- Sampling ---------------------------------------------------------
    if "applytopktopp" in text:
        categories.add("sampling.top_k_top_p")
        categories.add("sampling_or_selection")
        roles.add("sampling")
    if "argmax" in text:
        categories.add("sampling.argmax")
        categories.add("sampling_or_selection")
        roles.add("selection")

    # --- Normalisation + block_head heuristics (UI-only structure hint) ---
    # The "hc" / "mhc" prefix marks a structural block-head normalisation
    # helper that can prefix EITHER an attention block OR an MoE routing
    # block. We tag it as ``block_head.mhc_prefix`` regardless of which
    # block follows; downstream consumers must NOT treat the prefix alone
    # as evidence of MoE-only or attention-only context.
    is_collective = "communication.collective" in categories
    if not is_collective:
        if text.startswith("hc") or "mhc" in text:
            categories.add("block_head.mhc_prefix")
            roles.add("block_head")
    if "norm" in text:
        categories.add("normalization")
        roles.add("normalization")
        if "add" in text or "mhc" in text or (text.startswith("hc") and not is_collective):
            categories.add("block_head")
            roles.add("block_head")

    # --- AICPU ------------------------------------------------------------
    if any(token in text for token in ("aicpu", "ai_cpu")):
        categories.add("aicpu")
        roles.add("aicpu")

    result = (tuple(sorted(categories)), tuple(sorted(roles)))
    _CATEGORY_ROLE_CACHE[cache_key] = result
    return result


# ----------------------------------------------------------------------------
# Attention family resolver (category-driven)
# ----------------------------------------------------------------------------
# Single source of truth for the paper-aligned attention family label used
# by both ``html_report.detect_attention_subtype`` and the unit tests in
# ``tests/test_attention_families.py``. Keeping the decision logic here
# (instead of duplicating it as a private helper in either consumer)
# guarantees the test contract and the HTML report agree.
#
# Inputs are category labels emitted by ``categories_and_roles``, NOT raw
# kernel names — that way:
#   * metadata-only categories like ``attention.sparse_sharedkv.metadata``
#     can't masquerade as the main ``attention.sparse_sharedkv``
#     signature (regression-tested);
#   * future kernel renames touch one place (``categories_and_roles`` /
#     ``kernel_signatures.yaml``) and the resolver stays unchanged.
#
# Returns one of the paper-aligned family names:
#
#   * ``csa``         — DeepSeek-V4 main layers (Compressed Sparse Attention)
#   * ``hca``         — DeepSeek-V4 alternating layers (Heavily Compressed
#                       Attention; heuristic)
#   * ``dsa``         — DeepSeek-V3.2 (DeepSeek Sparse Attention, arxiv
#                       2512.02556)
#   * ``mla``         — DeepSeek-V2 / V3 (Multi-head Latent Attention)
#   * ``linear``      — Mamba / GDN / linear attention
#   * ``gqa_or_mha``  — dense flash-style attention via FIA /
#                       UnpadFlashAttention. Both kernels support
#                       MHA *and* GQA via the ``num_key_value_heads``
#                       parameter. This resolver function looks at
#                       *categories only* and therefore can't pick
#                       between MHA and GQA; it returns the umbrella
#                       ``gqa_or_mha``. A best-effort downstream step
#                       (``refine_dense_attention_from_shapes``) reads
#                       the Q/K Input Shapes recorded in
#                       ``kernel_details.csv`` and refines this to
#                       ``mha`` / ``gqa`` / ``mqa`` when shapes are
#                       available and pass sanity checks. The
#                       refinement is a heuristic — when shapes are
#                       missing or ambiguous, the report keeps the
#                       umbrella ``gqa_or_mha``.
#   * ``attn``        — unknown / unclassified
#
# An ``+kvc`` suffix is appended if the Hamming-distance KV-compression
# overlay is active (decode-only opt-in).
#
# Why ``attention.flash_score`` is neutral (not ``attention.gqa_or_mha``):
# the underlying CANN op
# (``aclnnFusedInferAttentionScore*`` / ``npu_fused_infer_attention_score``)
# is documented to handle MHA, GQA, AND MLA via parameter configuration.
# Naming the *kernel* category after one specific architecture would
# leak architecture inference into the kernel layer; we keep the kernel
# category neutral and resolve the architecture from the *combination*
# of categories present in a block.

_MLA_CATEGORIES = frozenset((
    "attention.mla",
    "attention.mla.preprocess",
    "attention.mla.kv_norm_rope_cache",
    "attention.mla.v_up_proj",
))

# Sanity-check guard for shape parsing: the last axis of Q/K tensors fed
# to FIA / UnpadFlashAttention is the per-head dim. If we accidentally
# pick up a mask, position table, or scale tensor, the "last axis" will
# rarely fall in this set, so we drop the candidate. Values cover the
# range seen across vLLM-Ascend supported models (dense path); MLA
# layers carry their own NoPE+RoPE concat head_dim values (192 / 576)
# but are resolved earlier in the decision order, so they don't reach
# the dense refinement path.
_VALID_HEAD_DIMS = frozenset({
    16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 192, 224, 256,
    320, 384, 448, 512, 576, 640, 768, 1024,
})

# vLLM model attention heads are within this range (sanity check).
_MAX_NUM_HEADS = 1024

# Names recognised as dense flash-attention score kernels for shape-based
# refinement. Keep in sync with the rule in ``categories_and_roles`` that
# emits ``attention.flash_score``.
#
# NOTE: The ATB ``pagedattention`` kernels also feed the dense flash-score
# path on Ascend (qwen25vl, glm45_0919, qwen25vl7b uses *both* this kernel
# AND ``UnpadFlashAttentionBF16NdKernel``). They were previously omitted
# from the token list which caused the shape-refinement pass to skip every
# qwen25vl/glm-0919 event silently. The refinement still returns the
# umbrella ``gqa_or_mha`` for paged-K layouts because num_kv_heads is not
# directly recoverable from the cache shape, but at least the events are
# now considered and the cases that DO carry non-paged shapes (UnpadFA
# prefill) can refine to mha / gqa / mqa.
_FLASH_SCORE_NAME_TOKENS = (
    "fusedinferattentionscore",
    "unpadflashattention",
    "flashattentionscore",
    "flashattention",
    "pagedattentionmask",
    "pagedattention",
)


def _parse_shape_token(token: str) -> list[int] | None:
    """Parse one CANN ``Input Shapes`` token into a list of positive
    integers. Returns ``None`` when the token is empty, malformed, or
    contains non-positive dims (which would mean a placeholder /
    optional input).
    """
    s = token.strip().strip('"').strip()
    if not s or s in ("()", "[]"):
        return None
    s = s.strip("()[]")
    parts = re.split(r"[,;\s]+", s)
    dims: list[int] = []
    for part in parts:
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            return None
        if value <= 0:
            return None
        dims.append(value)
    return dims or None


# When K is a 3D paged-KV-cache tensor ``[num_blocks, block_size, head_dim]``
# the original num_kv_heads has been folded into ``num_blocks * block_size``
# and we cannot recover it from shape alone. Heuristic: if K[0] is much
# larger than Q[0] we likely picked up a paged-cache K (e.g. Q=[4, 4, 256]
# vs K=[10000, 128, 256] on decode). The threshold below is conservative
# enough that batched-tokens Q vs concatenated-tokens K don't trip it
# (e.g. Q=[1620, 4, 128] vs K=[1620, 4, 128] passes).
_PAGED_K_BATCH_RATIO_GUARD = 8


def _qk_head_counts_from_input_shapes(
    input_shapes: Sequence[str],
) -> tuple[int, int] | None:
    """Pull ``(num_q_heads, num_kv_heads)`` from the first two valid
    Q/K tensors in a CANN ``Input Shapes`` list.

    The CANN ABI for both ``aclnnFusedInferAttentionScore[V2-V5]`` and
    ``UnpadFlashAttention`` puts ``query`` at input[0] and ``key`` at
    input[1]; we scan forward until we find two tensors whose last axis
    is a plausible per-head dim, then read the second-to-last axis as
    the head count. Returns ``None`` if the shapes don't satisfy the
    dense flash-attention invariant.

    Supported Q/K layouts:
      * 3D batch-major: ``[total_tokens, num_heads, head_dim]``
        (FIA prefill, UnpadFA non-paged).
      * 4D batched:     ``[B, S, num_heads, head_dim]``.

    Refuses to read:
      * 3D paged K-cache ``[num_blocks, block_size, head_dim]`` — here
        the second-to-last axis is ``block_size``, NOT ``num_kv_heads``;
        ``num_kv_heads`` has been folded into ``num_blocks * block_size``
        and is not directly recoverable. Detected via K[0] >> Q[0].
      * 5D+ tensors (unknown layout).
    """
    candidates: list[tuple[int, int, list[int]]] = []  # (num_heads, head_dim, dims)
    for token in input_shapes:
        dims = _parse_shape_token(token)
        if dims is None or len(dims) < 3 or len(dims) > 4:
            continue
        head_dim = dims[-1]
        num_heads = dims[-2]
        if head_dim not in _VALID_HEAD_DIMS:
            continue
        if num_heads < 1 or num_heads > _MAX_NUM_HEADS:
            continue
        candidates.append((num_heads, head_dim, dims))
        if len(candidates) >= 2:
            break
    if len(candidates) < 2:
        return None
    (num_q, head_dim_q, dims_q), (num_kv, head_dim_k, dims_k) = (
        candidates[0],
        candidates[1],
    )
    if head_dim_q != head_dim_k:
        # Q and K must share head_dim on FIA / UnpadFA — mismatch means
        # we latched onto the wrong tensor (mask / pse / etc).
        return None
    # Paged-K guard (decode direction): if both Q and K are 3D and K[0]
    # is much larger than Q[0], K is almost certainly a paged-cache
    # layout where the second-to-last axis is ``block_size``, not
    # ``num_kv_heads``. Bail out so we don't emit a wrong refinement.
    if (
        len(dims_q) == 3
        and len(dims_k) == 3
        and dims_q[0] > 0
        and dims_k[0] >= dims_q[0] * _PAGED_K_BATCH_RATIO_GUARD
    ):
        return None
    # Paged-K guard (prefill direction): in prefill mode Q carries the
    # total query token count and K is the paged cache. Q[0] can easily
    # exceed K[0] (e.g. Q=[32768,8,256], K=[1950,128,256] from nextprof),
    # so the decode-direction guard above doesn't fire. However, the
    # cache's second-to-last axis (which the loop reads as
    # ``num_kv_heads``) is really the block_size and is therefore much
    # larger than the true number of KV heads — and in real
    # MHA / GQA / MQA the invariant ``num_kv_heads <= num_q_heads``
    # always holds. So whenever the candidate Q/K pair violates that
    # invariant we are looking at a paged layout (or worse, the wrong
    # tensor) and must bail out instead of emitting a bogus refinement.
    if num_kv > num_q:
        return None
    return num_q, num_kv


def _split_cann_shapes_field(value: str) -> list[str]:
    """Replicates ``html_report._split_semi`` here so ``common.py`` stays
    independent of the report module. CANN ``Input Shapes`` is a
    ``;``-separated list; the cell may be quoted to escape inner ``;``.
    """
    if not value:
        return []
    v = value.strip()
    while len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
        if not v:
            break
    if not v:
        return []
    return [tok.strip() for tok in v.split(';')]


def refine_dense_attention_from_shapes(events: Iterable[Any]) -> str:
    """Best-effort upgrade of the terminal ``gqa_or_mha`` family label
    to ``mha`` / ``gqa`` / ``mqa`` using shapes recorded in
    ``kernel_details.csv:Input Shapes`` for FIA / UnpadFA events.

    Returns one of ``{'mha', 'gqa', 'mqa', 'gqa_or_mha'}``. The last
    value means refinement was not possible (shapes missing, malformed,
    or events disagreed without a clear majority); in that case the
    caller should keep the ``gqa_or_mha`` terminal label.

    **Best-effort, NOT a contract.** The skill does not read HF
    ``config.json``; this heuristic relies on the CANN profiler
    serialising the Q/K Input Shapes in the kernel_details row. That
    field can be missing or quirky after aclgraph compilation, so
    treat the refined sub-kind as an annotation, not a guarantee.

    Decision rules:
      * ``num_q_heads == num_kv_heads``                            → ``mha``
      * ``num_kv_heads == 1``  AND ``num_q_heads > 1``             → ``mqa``
      * ``num_q_heads > num_kv_heads`` AND ratio divides evenly    → ``gqa``
      * non-integer ratio, or shapes failed the sanity checks      → no vote
      * disagreement without a clear majority across events        → ``gqa_or_mha``
    """
    votes: dict[str, int] = {"mha": 0, "gqa": 0, "mqa": 0}
    for event in events:
        raw_name = getattr(event, "name", "") or ""
        text = raw_name.lower()
        if not any(token in text for token in _FLASH_SCORE_NAME_TOKENS):
            continue
        raw = getattr(event, "raw_row", None) or {}
        shapes_value = raw.get("Input Shapes") or raw.get("Input Shape") or ""
        if not shapes_value:
            continue
        tokens = _split_cann_shapes_field(str(shapes_value))
        head_counts = _qk_head_counts_from_input_shapes(tokens)
        if head_counts is None:
            continue
        num_q, num_kv = head_counts
        if num_q == num_kv and num_q >= 1:
            votes["mha"] += 1
        elif num_kv == 1 and num_q > 1:
            votes["mqa"] += 1
        elif num_q > num_kv and num_q % num_kv == 0:
            votes["gqa"] += 1
        # else: silent skip — odd ratios are usually parsing slip-ups

    total = sum(votes.values())
    if total == 0:
        return "gqa_or_mha"
    winner, score = max(votes.items(), key=lambda kv: kv[1])
    # Require an outright majority (not just plurality) so that a
    # ambiguous mix doesn't get a spuriously confident label.
    if score * 2 <= total:
        return "gqa_or_mha"
    return winner


def resolve_attention_family(categories: Iterable[str]) -> str:
    """Decide the paper-aligned attention family label from a set of
    category names emitted by ``categories_and_roles``.

    The decision order mirrors
    ``knowledge/attention_families.yaml:cheat_sheet`` exactly.
    """
    cats = set(categories)

    has_compressor = "attention.kv_compressor" in cats
    has_indexer = "attention.lightning_indexer" in cats
    has_sparse_sharedkv = "attention.sparse_sharedkv" in cats
    # NOTE: ``attention.sparse_sharedkv.metadata`` is deliberately not
    # treated as evidence of the main sparse signature — a metadata-only
    # block must not classify as DSA / CSA.
    has_flash_score = "attention.flash_score" in cats
    has_mla_marker = bool(_MLA_CATEGORIES & cats)

    if has_compressor and has_indexer and has_sparse_sharedkv:
        base = "csa"
    elif (
        has_compressor
        and has_flash_score
        and not has_indexer
        and not has_sparse_sharedkv
    ):
        base = "hca"
    elif has_indexer and has_sparse_sharedkv and not has_compressor:
        base = "dsa"
    elif has_mla_marker and not (
        has_compressor or has_indexer or has_sparse_sharedkv
    ):
        # MLA-architected layer. The flash_score kernel may or may not
        # be present (MLA decode reuses FIA for the score step); either
        # way the MLA-specific companions (MlaProlog / KvRmsNormRopeCache
        # / MLA V-up-proj) take precedence over the bare flash_score
        # signal.
        base = "mla"
    elif "attention.linear_or_mamba" in cats:
        base = "linear"
    elif has_flash_score:
        # Dense flash-style umbrella label. FIA / UnpadFlashAttention
        # without any architecture-specific companion. From categories
        # alone we can't pick between MHA / GQA / MQA, so the resolver
        # returns the umbrella ``gqa_or_mha``. The caller (currently
        # ``html_report.detect_attention_subtype``) may then invoke
        # ``refine_dense_attention_from_shapes`` to upgrade this to
        # ``mha`` / ``gqa`` / ``mqa`` using the Q/K Input Shapes
        # recorded in ``kernel_details.csv``. That refinement step is
        # best-effort — when shapes are missing or fail sanity checks
        # the label stays ``gqa_or_mha``.
        base = "gqa_or_mha"
    else:
        base = "attn"

    if "attention.kvcomp.topk" in cats:
        base = f"{base}+kvc"
    return base


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
