#!/usr/bin/env python3
"""Best-effort per-event FLOP / byte estimates from CANN shape fields.

Deliberately conservative: unknown kernels keep byte counts when shapes
exist, but FLOPs stay unset unless a known matmul / attention / vector
pattern matches. All figures are ranking lenses, not evidence-chain
conclusions.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

try:
    from .store import fold_text, pick, try_float
except ImportError:  # pragma: no cover - script-mode fallback
    from store import fold_text, pick, try_float  # type: ignore[no-redef]


_NULLISH_TEXT = {"", "N/A", "NA", "NAN", "NONE", "NULL"}

# Candidate column aliases for the shape / dtype cells consumed by
# ``estimated_work_from_row`` (single source of truth; the normalize hot
# path resolves them once per file via ``sources.KernelRowAccessor``).
INPUT_SHAPE_CANDIDATES = ("Input Shapes", "Input Shape", "Input")
OUTPUT_SHAPE_CANDIDATES = ("Output Shapes", "Output Shape", "Output")
INPUT_DTYPE_CANDIDATES = ("Input Data Types", "Input Data Type", "Input Dtypes")
OUTPUT_DTYPE_CANDIDATES = ("Output Data Types", "Output Data Type", "Output Dtypes")


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


def estimated_work_from_fields(
    *,
    name: str,
    task_type: str,
    op_type: str,
    input_shapes_raw: str,
    output_shapes_raw: str,
    input_dtypes_raw: str,
    output_dtypes_raw: str,
) -> dict[str, Any]:
    """``estimated_work_from_row`` over already-picked raw cell strings.

    The estimate depends only on these raw strings plus ``name`` /
    ``task_type`` / ``op_type``, so hot loops can memoize on the raw
    (unparsed) values and skip shape/dtype parsing for repeated
    combinations.
    """

    input_shapes = parse_tensor_shapes(input_shapes_raw)
    output_shapes = parse_tensor_shapes(output_shapes_raw)
    input_dtypes = parse_tensor_dtypes(input_dtypes_raw)
    output_dtypes = parse_tensor_dtypes(output_dtypes_raw)
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


def estimated_work_from_row(row: Mapping[str, Any], *, name: str, task_type: str, op_type: str) -> dict[str, Any]:
    """Estimate per-event bytes/FLOPs from raw CANN shape and dtype fields.

    This adapts the useful part of LLMInsight's operator calculation
    model into the profiling pipeline.  It is deliberately conservative:
    unknown kernels keep byte counts when shapes exist, but FLOPs stay
    unset unless a known matmul/attention/vector pattern matches.
    """

    return estimated_work_from_fields(
        name=name,
        task_type=task_type,
        op_type=op_type,
        input_shapes_raw=pick(row, INPUT_SHAPE_CANDIDATES, ""),
        output_shapes_raw=pick(row, OUTPUT_SHAPE_CANDIDATES, ""),
        input_dtypes_raw=pick(row, INPUT_DTYPE_CANDIDATES, ""),
        output_dtypes_raw=pick(row, OUTPUT_DTYPE_CANDIDATES, ""),
    )
