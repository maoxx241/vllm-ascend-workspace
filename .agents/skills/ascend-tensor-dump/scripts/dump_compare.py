#!/usr/bin/env python3
"""Read and compare bounded tensor dumps produced by ``assets/dump_probe.py``.

Three subcommands, ordered by cost:

``scan``     Read one or more JSON manifests. Report the stage order, the first
             stage that contains NaN or Inf, the first stage whose magnitude
             exceeds a limit, and any stages that share device storage. Needs
             no tensors and no torch.
``diff``     Pair two manifests stage by stage and report the first divergent
             stage from the device-side statistics alone. Needs no tensors and
             no torch. This is the workhorse.
``tensors``  Pair two ``.pt`` payloads and report full per-tensor metrics.
             Imports torch only to load the files; every metric is computed in
             plain Python so the dump size stays the thing you budget.

Progress goes to stderr, the final JSON object goes to stdout.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

STAT_NAMES = ("nan_count", "inf_count", "max_abs", "min", "max", "mean")

DEFAULT_MAX_ELEMENTS = 2_000_000


class DumpCompareError(Exception):
    """Raised for unusable inputs. Reported as a failed status, not a crash."""


def emit_progress(phase: str, **details: Any) -> None:
    print(json.dumps({"phase": phase, **details}, ensure_ascii=False), file=sys.stderr)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DumpCompareError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DumpCompareError(f"manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise DumpCompareError(f"manifest has no records list: {path}")
    return payload


# --------------------------------------------------------------------------
# keys and pairing
# --------------------------------------------------------------------------


def record_keys(records: Iterable[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Give every record a stable key of ``stage#occurrence``.

    A stage can legitimately appear several times in one forward (per layer,
    per operator call). Occurrence is assigned in capture order so both sides
    of a comparison line up without any hashing.
    """
    seen: dict[str, int] = {}
    keyed: list[tuple[str, dict[str, Any]]] = []
    for record in records:
        stage = str(record.get("stage", ""))
        index = seen.get(stage, 0)
        seen[stage] = index + 1
        keyed.append((f"{stage}#{index}", record))
    return keyed


def _nonfinite(summary: Any) -> float:
    if not isinstance(summary, dict):
        return 0.0
    return float(summary.get("nan_count", 0) or 0) + float(
        summary.get("inf_count", 0) or 0
    )


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def storage_aliases(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group stages that share one device allocation.

    Two stages sharing ``storage_ptr`` is normal for views. Sharing it with
    *different strides or offsets* is how cache and block-table collisions show
    up, and it is invisible once a tensor has been made contiguous. Both cases
    are reported; ``stride_conflict`` marks the interesting one.
    """
    groups: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        pointer = record.get("storage_ptr")
        if not isinstance(pointer, int) or pointer == 0:
            continue
        groups.setdefault(pointer, []).append(record)

    aliases: list[dict[str, Any]] = []
    for pointer, members in sorted(groups.items()):
        stages = sorted({str(member.get("stage", "")) for member in members})
        if len(stages) < 2:
            continue
        layouts = {
            (
                tuple(member.get("stride") or ()),
                int(member.get("storage_offset") or 0),
            )
            for member in members
        }
        aliases.append(
            {
                "storage_ptr": pointer,
                "stages": stages,
                "stride_conflict": len(layouts) > 1,
                "layouts": [
                    {"stride": list(stride), "storage_offset": offset}
                    for stride, offset in sorted(layouts)
                ],
            }
        )
    return aliases


def scan_manifest(path: Path, *, max_abs_limit: float | None) -> dict[str, Any]:
    manifest = load_manifest(path)
    records = manifest["records"]

    first_nonfinite: dict[str, Any] | None = None
    first_over_limit: dict[str, Any] | None = None
    missing_summary: list[str] = []

    for key, record in record_keys(records):
        summary = record.get("summary")
        if not isinstance(summary, dict):
            if "inputs" not in record:
                missing_summary.append(key)
            continue
        if first_nonfinite is None and _nonfinite(summary) > 0:
            first_nonfinite = {
                "key": key,
                "stage": record.get("stage"),
                "order": record.get("order"),
                "nan_count": summary.get("nan_count"),
                "inf_count": summary.get("inf_count"),
            }
        if (
            first_over_limit is None
            and max_abs_limit is not None
            and float(summary.get("max_abs", 0) or 0) > max_abs_limit
        ):
            first_over_limit = {
                "key": key,
                "stage": record.get("stage"),
                "order": record.get("order"),
                "max_abs": summary.get("max_abs"),
            }

    return {
        "path": str(path),
        "label": manifest.get("label"),
        "rank": manifest.get("rank"),
        "match_index": manifest.get("match_index"),
        "occurrence": manifest.get("occurrence"),
        "metadata": manifest.get("metadata", {}),
        "record_count": len(records),
        "tensor_file": manifest.get("tensor_file"),
        "graph_slots": sorted(manifest.get("graph_slots", {})),
        "first_nonfinite": first_nonfinite,
        "first_over_limit": first_over_limit,
        "records_without_summary": missing_summary,
        "storage_aliases": storage_aliases(records),
    }


def scan(paths: list[Path], *, max_abs_limit: float | None) -> dict[str, Any]:
    results = []
    for path in paths:
        emit_progress("scan", path=str(path))
        results.append(scan_manifest(path, max_abs_limit=max_abs_limit))
    flagged = [
        result["path"]
        for result in results
        if result["first_nonfinite"] or result["first_over_limit"]
    ]
    return {
        "status": "ok",
        "command": "scan",
        "manifests": results,
        "flagged": flagged,
    }


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


def _stat_diverged(
    left: Any, right: Any, *, atol: float, rtol: float
) -> dict[str, Any] | None:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    diverged: dict[str, Any] = {}
    for name in STAT_NAMES:
        if name not in left or name not in right:
            continue
        try:
            left_value = float(left[name])
            right_value = float(right[name])
        except (TypeError, ValueError):
            continue
        if math.isnan(left_value) and math.isnan(right_value):
            continue
        tolerance = atol + rtol * abs(right_value)
        if abs(left_value - right_value) > tolerance:
            diverged[name] = {
                "left": left_value,
                "right": right_value,
                "delta": left_value - right_value,
            }
    return diverged or None


def diff_manifests(
    left_path: Path,
    right_path: Path,
    *,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    left = load_manifest(left_path)
    right = load_manifest(right_path)

    left_keyed = dict(record_keys(left["records"]))
    right_keyed = dict(record_keys(right["records"]))
    order = [key for key, _ in record_keys(left["records"])]

    stages: list[dict[str, Any]] = []
    first_divergent: dict[str, Any] | None = None

    for key in order:
        if key not in right_keyed:
            continue
        left_record = left_keyed[key]
        right_record = right_keyed[key]

        shape_match = list(left_record.get("shape") or []) == list(
            right_record.get("shape") or []
        )
        dtype_match = left_record.get("dtype") == right_record.get("dtype")
        left_summary = left_record.get("summary")
        right_summary = right_record.get("summary")
        stat_divergence = _stat_diverged(
            left_summary, right_summary, atol=atol, rtol=rtol
        )
        nonfinite_introduced = _nonfinite(right_summary) > _nonfinite(left_summary)

        entry = {
            "key": key,
            "stage": left_record.get("stage"),
            "order": left_record.get("order"),
            "shape_match": shape_match,
            "dtype_match": dtype_match,
            "left_shape": left_record.get("shape"),
            "right_shape": right_record.get("shape"),
            "nonfinite_introduced": nonfinite_introduced,
            "stat_divergence": stat_divergence,
        }
        stages.append(entry)

        if first_divergent is None and (
            not shape_match or nonfinite_introduced or stat_divergence
        ):
            reasons = []
            if not shape_match:
                reasons.append("shape")
            if nonfinite_introduced:
                reasons.append("nonfinite")
            if stat_divergence:
                reasons.append("statistics")
            first_divergent = {**entry, "reasons": reasons}

    return {
        "status": "ok",
        "command": "diff",
        "left": {
            "path": str(left_path),
            "label": left.get("label"),
            "rank": left.get("rank"),
        },
        "right": {
            "path": str(right_path),
            "label": right.get("label"),
            "rank": right.get("rank"),
        },
        "atol": atol,
        "rtol": rtol,
        "compared": len(stages),
        "only_in_left": sorted(set(left_keyed) - set(right_keyed)),
        "only_in_right": sorted(set(right_keyed) - set(left_keyed)),
        "first_divergent": first_divergent,
        "stages": stages,
        "verdict": "DIVERGENT" if first_divergent else "ALIGNED",
    }


# --------------------------------------------------------------------------
# tensors
# --------------------------------------------------------------------------


def flatten_payload(payload: Any, *, label: str) -> dict[str, Any]:
    """Turn a probe ``.pt`` payload into a flat key to tensor mapping."""
    if not isinstance(payload, dict):
        raise DumpCompareError(f"{label}: payload is not a dict")

    flat: dict[str, Any] = {}
    seen: dict[str, int] = {}

    def add(key: str, value: Any) -> None:
        index = seen.get(key, 0)
        seen[key] = index + 1
        flat[f"{key}#{index}"] = value

    for stage, tensor in payload.get("tensors") or []:
        add(str(stage), tensor)
    for stage, inputs in payload.get("inputs") or []:
        if not isinstance(inputs, dict):
            continue
        for name, value in inputs.items():
            add(f"{stage}:{name}", value)
    for name, tensor in (payload.get("graph_slots") or {}).items():
        add(f"graph:{name}", tensor)
    return flat


def to_floats(tensor: Any, *, max_elements: int) -> tuple[list[float], dict[str, Any]]:
    """Flatten a tensor to Python floats, or refuse if it is too large."""
    import torch

    if not isinstance(tensor, torch.Tensor):
        return [], {"kind": "non-tensor", "value": repr(tensor)}
    info = {
        "kind": "tensor",
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype),
        "numel": int(tensor.numel()),
    }
    if tensor.numel() > max_elements:
        info["skipped"] = (
            f"{tensor.numel()} elements exceeds --max-elements {max_elements}; "
            "lower DUMP_PROBE_ROWS or raise the limit deliberately"
        )
        return [], info
    return tensor.reshape(-1).to(torch.float64).tolist(), info


def tensor_metrics(
    left: list[float],
    right: list[float],
    *,
    integral: bool,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    """Compute comparison metrics in plain Python.

    Non-finite values are reported, not folded into the metrics: once one side
    has a NaN the answer is already "this stage", and averaging over it would
    hide that.
    """
    left_nonfinite = sum(1 for value in left if not math.isfinite(value))
    right_nonfinite = sum(1 for value in right if not math.isfinite(value))
    metrics: dict[str, Any] = {
        "left_nonfinite": left_nonfinite,
        "right_nonfinite": right_nonfinite,
    }
    if left_nonfinite or right_nonfinite:
        metrics["comparable"] = False
        return metrics

    metrics["comparable"] = True
    mismatch = sum(1 for a, b in zip(left, right) if a != b)
    metrics["exact_equal"] = mismatch == 0
    metrics["mismatch_count"] = mismatch
    if integral or not left:
        return metrics

    diffs = [abs(a - b) for a, b in zip(left, right)]
    metrics["max_abs_diff"] = max(diffs)
    metrics["mean_abs_diff"] = sum(diffs) / len(diffs)
    metrics["allclose"] = all(
        diff <= atol + rtol * abs(b) for diff, b in zip(diffs, right)
    )

    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    dot = sum(a * b for a, b in zip(left, right))
    if left_norm > 0 and right_norm > 0:
        metrics["cosine"] = dot / (left_norm * right_norm)
    if right_norm > 0:
        metrics["rel_l2"] = math.sqrt(sum(d * d for d in diffs)) / right_norm
    return metrics


def compare_tensor_payloads(
    left_payload: Any,
    right_payload: Any,
    *,
    atol: float,
    rtol: float,
    max_elements: int,
) -> dict[str, Any]:
    left_flat = flatten_payload(left_payload, label="left")
    right_flat = flatten_payload(right_payload, label="right")

    items: list[dict[str, Any]] = []
    first_mismatch: dict[str, Any] | None = None

    for key in left_flat:
        if key not in right_flat:
            continue
        left_values, left_info = to_floats(left_flat[key], max_elements=max_elements)
        right_values, right_info = to_floats(right_flat[key], max_elements=max_elements)

        entry: dict[str, Any] = {
            "key": key,
            "left": left_info,
            "right": right_info,
        }
        shape_match = left_info.get("shape") == right_info.get("shape")
        dtype_match = left_info.get("dtype") == right_info.get("dtype")
        entry["shape_match"] = shape_match
        entry["dtype_match"] = dtype_match

        if left_info.get("kind") != "tensor" or right_info.get("kind") != "tensor":
            entry["metrics"] = {
                "comparable": False,
                "reason": "at least one side is not a tensor",
            }
        elif not shape_match:
            entry["metrics"] = {"comparable": False, "reason": "shape mismatch"}
        elif "skipped" in left_info or "skipped" in right_info:
            entry["metrics"] = {"comparable": False, "reason": "too large"}
        else:
            dtype = str(left_info.get("dtype", ""))
            integral = "int" in dtype or "bool" in dtype
            entry["metrics"] = tensor_metrics(
                left_values,
                right_values,
                integral=integral,
                atol=atol,
                rtol=rtol,
            )

        items.append(entry)

        metrics = entry["metrics"]
        failed = (
            not shape_match
            or metrics.get("comparable") is False
            or metrics.get("allclose") is False
            or (metrics.get("allclose") is None and metrics.get("exact_equal") is False)
        )
        if first_mismatch is None and failed:
            first_mismatch = entry

    return {
        "status": "ok",
        "command": "tensors",
        "atol": atol,
        "rtol": rtol,
        "compared": len(items),
        "only_in_left": sorted(set(left_flat) - set(right_flat)),
        "only_in_right": sorted(set(right_flat) - set(left_flat)),
        "first_mismatch": first_mismatch,
        "items": items,
        "verdict": "FAIL" if first_mismatch else "PASS",
    }


def compare_tensor_files(
    left_path: Path,
    right_path: Path,
    *,
    atol: float,
    rtol: float,
    max_elements: int,
) -> dict[str, Any]:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - depends on host
        raise DumpCompareError(
            "the tensors subcommand needs torch; run it where the dumps live "
            "or use scan/diff on the JSON manifests instead"
        ) from exc

    for path in (left_path, right_path):
        if not path.exists():
            raise DumpCompareError(f"tensor file not found: {path}")

    emit_progress("load", left=str(left_path), right=str(right_path))
    left_payload = torch.load(left_path, map_location="cpu", weights_only=False)
    right_payload = torch.load(right_path, map_location="cpu", weights_only=False)

    result = compare_tensor_payloads(
        left_payload,
        right_payload,
        atol=atol,
        rtol=rtol,
        max_elements=max_elements,
    )
    result["left"] = str(left_path)
    result["right"] = str(right_path)
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan", help="report stage order, first non-finite stage and storage aliases"
    )
    scan_parser.add_argument("--manifest", required=True, nargs="+", type=Path)
    scan_parser.add_argument(
        "--max-abs-limit",
        type=float,
        default=None,
        help="flag the first stage whose max_abs exceeds this value",
    )

    diff_parser = subparsers.add_parser(
        "diff", help="pair two manifests and find the first divergent stage"
    )
    diff_parser.add_argument("--left", required=True, type=Path)
    diff_parser.add_argument("--right", required=True, type=Path)
    diff_parser.add_argument("--atol", type=float, default=0.0)
    diff_parser.add_argument("--rtol", type=float, default=0.0)

    tensors_parser = subparsers.add_parser(
        "tensors", help="compare two .pt payloads tensor by tensor"
    )
    tensors_parser.add_argument("--left", required=True, type=Path)
    tensors_parser.add_argument("--right", required=True, type=Path)
    tensors_parser.add_argument("--atol", type=float, default=1e-2)
    tensors_parser.add_argument("--rtol", type=float, default=1e-2)
    tensors_parser.add_argument(
        "--max-elements", type=int, default=DEFAULT_MAX_ELEMENTS
    )

    for subparser in (diff_parser, tensors_parser):
        subparser.add_argument(
            "--fail-on-divergence",
            action="store_true",
            help="exit 1 when the comparison is not aligned, for gating",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            payload = scan(args.manifest, max_abs_limit=args.max_abs_limit)
        elif args.command == "diff":
            payload = diff_manifests(
                args.left, args.right, atol=args.atol, rtol=args.rtol
            )
        else:
            payload = compare_tensor_files(
                args.left,
                args.right,
                atol=args.atol,
                rtol=args.rtol,
                max_elements=args.max_elements,
            )
    except DumpCompareError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps(payload, ensure_ascii=False))
    if getattr(args, "fail_on_divergence", False) and payload.get("verdict") in {
        "DIVERGENT",
        "FAIL",
    }:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
