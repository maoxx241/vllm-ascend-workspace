#!/usr/bin/env python3
"""Sweep the new Ascend profiling analysis pipeline over profiling roots."""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .analyze import analyze_profile
    from .common import (
        SCHEMA_VERSION,
        TOOL_VERSION,
        csv_rows,
        emit_stage_json,
        read_json,
        stable_id,
        utc_now,
        write_csv,
        write_json,
    )
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from analyze import analyze_profile  # type: ignore[no-redef]
    from common import (  # type: ignore[no-redef]
        SCHEMA_VERSION,
        TOOL_VERSION,
        csv_rows,
        emit_stage_json,
        read_json,
        stable_id,
        utc_now,
        write_csv,
        write_json,
    )


def find_kernel_details(search_root: Path) -> list[Path]:
    if not search_root.exists():
        return []
    ignored_parts = {"profile_analysis", "ascend_profile_framework", "__pycache__"}
    return sorted(
        path
        for path in search_root.rglob("kernel_details.csv")
        if not any(part in ignored_parts or ".bak" in part or part.startswith("bak_") for part in path.parts)
    )


def rank_dir_from_csv(csv_path: Path) -> Path:
    return csv_path.parent.parent if csv_path.parent.name == "ASCEND_PROFILER_OUTPUT" else csv_path.parent


def has_direct_kernel_details(path: Path) -> bool:
    return (path / "kernel_details.csv").is_file() or (path / "ASCEND_PROFILER_OUTPUT" / "kernel_details.csv").is_file()


def looks_like_rank_dir(path: Path) -> bool:
    name = path.name.lower()
    return bool(re.search(r"(^rank\d+|_rank\d+|^dp\d+_pp\d+_tp\d+|_ascend_pt$)", name))


def case_root_from_rank(rank_dir: Path) -> Path:
    parent = rank_dir.parent
    if parent.name in {"vllm_profile", "vllm_profile_rank"}:
        return parent
    if not looks_like_rank_dir(rank_dir):
        return rank_dir
    return parent


def discover_roots(search_roots: Sequence[Path]) -> list[Path]:
    roots: set[Path] = set()
    for search_root in search_roots:
        if has_direct_kernel_details(search_root):
            roots.add(search_root.resolve())
        for csv_path in find_kernel_details(search_root):
            roots.add(case_root_from_rank(rank_dir_from_csv(csv_path)).resolve())
    return sorted(roots, key=lambda item: str(item))


def safe_slug(root: Path) -> str:
    text = str(root).strip("/").replace("/", "__")
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text[-180:] or stable_id("root", root)


def count_by(rows: Sequence[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def step_inventory(output_dir: Path) -> dict[str, Any]:
    payload = read_json(output_dir / "step_segments.json", default={}) or {}
    rows = payload.get("step_segments") or []
    by_rank: dict[str, dict[str, int]] = {}
    union: set[int] = set()
    for row in rows:
        if row.get("segment_type") != "step":
            continue
        rank_id = str(row.get("rank_id") or "unknown")
        layer_count = row.get("main_layer_count")
        if layer_count is None:
            key = "none"
        else:
            key = str(int(layer_count))
            union.add(int(layer_count))
        rank_counts = by_rank.setdefault(rank_id, {})
        rank_counts[key] = rank_counts.get(key, 0) + 1
    return {
        "rank_layer_inventory": {rank: dict(sorted(counts.items(), key=lambda item: int(item[0]) if item[0].isdigit() else -1)) for rank, counts in sorted(by_rank.items())},
        "union_layers": sorted(union),
    }


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def cross_root_rollup_rows(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build a one-row-per-root cross-root comparison table.

    Each row captures the root's "headline" numbers so the user can
    eyeball performance across configurations without opening every
    individual report:

    - ``rank_count`` / ``event_count`` / ``step_count`` -- capture scale
    - ``top_step_class_*`` -- the heaviest step class (members × wall mean)
      with its wall p50/p90 and bubble share
    - ``block_kind_wall_share`` -- attention / ffn / moe wall split for
      the *whole* root (sum across blocks)
    - ``hccl_*`` -- total HCCL wall, share of root wall, and the
      heaviest HCCL kind plus its rank skew

    Missing data is rendered as zero / empty so consumers can sort.
    """

    rollup_rows: list[dict[str, Any]] = []
    for entry in results:
        if entry.get("status") != "ok":
            rollup_rows.append({"root": entry.get("root"), "status": entry.get("status"), "error": entry.get("error")})
            continue
        root = entry.get("root")
        out_dir = Path(entry.get("output_dir") or "")
        if not out_dir.exists():
            rollup_rows.append({"root": root, "status": "missing_output", "error": str(out_dir)})
            continue

        normalize_manifest = read_json(out_dir / "normalize_manifest.json", default={}) or {}
        rank_summary = csv_rows(out_dir / "rank_summary.csv")
        wall_total_ms = sum(_f(row.get("wall_ms")) for row in rank_summary)
        step_total = sum(int(row.get("step_count") or 0) for row in rank_summary)

        # Heaviest step class.
        step_class_rows = csv_rows(out_dir / "step_class_summary.csv")
        heaviest_step = None
        heaviest_step_score = -1.0
        for row in step_class_rows:
            score = _f(row.get("wall_ms_mean")) * float(row.get("member_count") or 0)
            if score > heaviest_step_score:
                heaviest_step_score = score
                heaviest_step = row

        # Block-kind wall share across the whole root.
        block_class_rows = csv_rows(out_dir / "block_class_summary.csv")
        kind_total: dict[str, float] = {}
        kind_total_grand = 0.0
        for row in block_class_rows:
            kind = str(row.get("block_kind") or "other")
            wall_sum = _f(row.get("wall_ms_sum"))
            kind_total[kind] = kind_total.get(kind, 0.0) + wall_sum
            kind_total_grand += wall_sum
        kind_share: dict[str, float] = {}
        for kind, value in kind_total.items():
            kind_share[kind] = round(value / kind_total_grand, 6) if kind_total_grand > 0 else 0.0

        # HCCL summary.
        hccl_class_rows = csv_rows(out_dir / "hccl_class_summary.csv")
        hccl_wall_ms = sum(_f(row.get("duration_sum_us")) for row in hccl_class_rows) / 1000.0
        heaviest_hccl = None
        heaviest_hccl_score = -1.0
        for row in hccl_class_rows:
            score = _f(row.get("duration_sum_us"))
            if score > heaviest_hccl_score:
                heaviest_hccl_score = score
                heaviest_hccl = row
        max_skew = max((_f(row.get("rank_skew_ratio")) for row in hccl_class_rows), default=0.0)

        rollup_row = {
            "root": root,
            "output_dir": str(out_dir),
            "rank_count": entry.get("rank_count"),
            "event_count": entry.get("event_count"),
            "segment_count": entry.get("segment_count"),
            "layer_count": entry.get("layer_count"),
            "union_layers": entry.get("union_layers"),
            "step_count": step_total,
            "wall_ms_sum": round(wall_total_ms, 3),
            "elapsed_s": entry.get("elapsed_s"),
            "diagnosis_counts": entry.get("diagnosis_counts"),
            "step_class_count": len(step_class_rows),
            "block_class_count": len(block_class_rows),
            "top_step_class_id": (heaviest_step or {}).get("step_class_id"),
            "top_step_family": (heaviest_step or {}).get("step_family"),
            "top_step_members": (heaviest_step or {}).get("member_count"),
            "top_step_wall_ms_mean": _f((heaviest_step or {}).get("wall_ms_mean")),
            "top_step_wall_ms_p50": _f((heaviest_step or {}).get("wall_ms_p50")),
            "top_step_wall_ms_p90": _f((heaviest_step or {}).get("wall_ms_p90")),
            "top_step_bubble_ratio_mean": _f((heaviest_step or {}).get("bubble_ratio_mean")),
            "block_kind_wall_share": kind_share,
            "block_kind_wall_ms_sum": kind_total,
            "hccl_total_ms": round(hccl_wall_ms, 3),
            "hccl_share_of_wall": round(hccl_wall_ms / wall_total_ms, 6) if wall_total_ms > 0 else 0.0,
            "hccl_top_kind": (heaviest_hccl or {}).get("hccl_op_kind"),
            "hccl_top_comm_aiv_fused": (heaviest_hccl or {}).get("comm_aiv_fused"),
            "hccl_top_calls": int((heaviest_hccl or {}).get("call_count") or 0),
            "hccl_top_duration_ms": _f((heaviest_hccl or {}).get("duration_sum_us")) / 1000.0,
            "hccl_top_rank_skew_ratio": _f((heaviest_hccl or {}).get("rank_skew_ratio")),
            "hccl_max_rank_skew_ratio": round(max_skew, 6),
            "profile_root_label": normalize_manifest.get("profile_root"),
        }
        rollup_rows.append(rollup_row)
    return rollup_rows


def _analyze_one(
    idx: int,
    total: int,
    root: Path,
    output_dir: Path,
    *,
    verbose: bool,
    skip_html: bool,
    report_mode: str,
    reuse_existing: bool,
) -> dict[str, Any]:
    root_out = output_dir / safe_slug(root)
    item: dict[str, Any] = {
        "root": str(root),
        "output_dir": str(root_out),
        "status": None,
        "elapsed_s": None,
    }
    print(f"[{idx}/{total}] analyze {root}", flush=True)
    t0 = time.time()
    # Reuse path: if manifest.json already exists and reuse_existing is set,
    # don't re-run the pipeline. Useful when a sweep was interrupted and you
    # only want to fill in the missing roots.
    manifest_path = root_out / "manifest.json"
    if reuse_existing and manifest_path.is_file():
        try:
            manifest = read_json(manifest_path, default={}) or {}
            item.update({
                "status": "ok",
                "elapsed_s": 0.0,
                "reused": True,
                "stage_timings": manifest.get("stage_timings"),
                "rank_count": manifest.get("stage_results", {}).get("normalize", {}).get("rank_count"),
                "event_count": manifest.get("stage_results", {}).get("normalize", {}).get("event_count"),
                "segment_count": manifest.get("stage_results", {}).get("segment", {}).get("segment_count"),
                "layer_count": manifest.get("stage_results", {}).get("segment", {}).get("layer_count"),
                "diagnosis_counts": manifest.get("stage_results", {}).get("diagnostics", {}).get("counts"),
                **step_inventory(root_out),
            })
            return item
        except Exception:  # noqa: BLE001
            # Fall through to re-analyze if reuse fails for any reason.
            pass

    try:
        manifest = analyze_profile(
            root,
            root_out,
            verbose=verbose,
            skip_html=skip_html,
            report_mode=report_mode,
        )
        item.update(
            {
                "status": "ok",
                "elapsed_s": round(time.time() - t0, 6),
                "stage_timings": manifest.get("stage_timings"),
                "rank_count": manifest.get("stage_results", {}).get("normalize", {}).get("rank_count"),
                "event_count": manifest.get("stage_results", {}).get("normalize", {}).get("event_count"),
                "segment_count": manifest.get("stage_results", {}).get("segment", {}).get("segment_count"),
                "layer_count": manifest.get("stage_results", {}).get("segment", {}).get("layer_count"),
                "diagnosis_counts": manifest.get("stage_results", {}).get("diagnostics", {}).get("counts"),
                **step_inventory(root_out),
            }
        )
    except Exception as exc:  # noqa: BLE001
        item.update({"status": "error", "elapsed_s": round(time.time() - t0, 6), "error": repr(exc)})
    return item


def sweep_roots(
    search_roots: Sequence[Path],
    output_dir: Path,
    *,
    limit: int | None,
    verbose: bool,
    jobs: int = 1,
    skip_html: bool = True,
    report_mode: str = "summary",
    reuse_existing: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    roots = discover_roots(search_roots)
    if limit is not None:
        roots = roots[: max(0, limit)]
    started = time.time()
    results: list[dict[str, Any]] = []
    total = len(roots)

    if jobs <= 1 or total <= 1:
        for idx, root in enumerate(roots, 1):
            results.append(
                _analyze_one(
                    idx, total, root, output_dir,
                    verbose=verbose, skip_html=skip_html,
                    report_mode=report_mode, reuse_existing=reuse_existing,
                )
            )
    else:
        # Use a thread pool: each worker forks the analysis pipeline in this
        # interpreter. The pipeline is CPU-bound (CSV/JSON parsing + Python
        # rollups), so true parallelism with a pool of threads is limited by
        # the GIL, but in practice each root spends a lot of time in IO
        # (reading large CSVs, writing JSON), so 2-4 workers help noticeably
        # on multi-root sweeps. For heavier parallelism users can run the
        # sweep wrapper multiple times against disjoint --search-root sets.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max(1, int(jobs))) as pool:
            future_to_idx = {
                pool.submit(
                    _analyze_one,
                    idx, total, root, output_dir,
                    verbose=verbose, skip_html=skip_html,
                    report_mode=report_mode, reuse_existing=reuse_existing,
                ): idx
                for idx, root in enumerate(roots, 1)
            }
            done = [None] * total
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                done[idx - 1] = future.result()
            results.extend(item for item in done if item is not None)

    rollup_rows = cross_root_rollup_rows(results)
    write_csv(output_dir / "sweep_class_rollup.csv", rollup_rows)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "sweep",
        "created_at": utc_now(),
        "search_roots": [str(root) for root in search_roots],
        "output_dir": str(output_dir),
        "root_count": len(roots),
        "elapsed_s": round(time.time() - started, 6),
        "status_counts": count_by(results, "status"),
        "config": {
            "jobs": int(jobs),
            "skip_html": bool(skip_html),
            "report_mode": report_mode,
            "reuse_existing": bool(reuse_existing),
        },
        "files": {
            "sweep_summary": "sweep_summary.json",
            "sweep_class_rollup": "sweep_class_rollup.csv",
        },
        "results": results,
        "rollup_row_count": len(rollup_rows),
    }
    write_json(output_dir / "sweep_summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-root", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help=(
            "number of roots to analyze in parallel (thread pool). Default 1. "
            "Practical sweet spot 2-4 on a fast machine; the pipeline is a mix "
            "of IO and CPU and the GIL caps true parallelism."
        ),
    )
    parser.add_argument(
        "--skip-html",
        action="store_true",
        default=True,
        help=(
            "skip per-root HTML rendering. ON by default for sweeps because "
            "HTML can be 100MB+ per root; turn off with --no-skip-html if you "
            "really want HTML for every root."
        ),
    )
    parser.add_argument(
        "--no-skip-html",
        dest="skip_html",
        action="store_false",
        help="render HTML for each root (opposite of --skip-html)",
    )
    parser.add_argument(
        "--report-mode",
        choices=("summary", "interactive", "full-raw"),
        default="summary",
        help=(
            "per-root HTML report mode when --no-skip-html is set. "
            "Defaults to 'summary' to keep sweep outputs small."
        ),
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help=(
            "reuse a prior root's output if its manifest.json already exists. "
            "Lets you continue an interrupted sweep without redoing finished roots."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = sweep_roots(
        [Path(item) for item in args.search_root],
        Path(args.output),
        limit=args.limit,
        verbose=bool(args.verbose),
        jobs=int(args.jobs),
        skip_html=bool(args.skip_html),
        report_mode=args.report_mode,
        reuse_existing=bool(args.reuse_existing),
    )
    emit_stage_json(
        {
            "stage": "sweep",
            "root_count": summary["root_count"],
            "elapsed_s": summary["elapsed_s"],
            "status_counts": summary["status_counts"],
            "config": summary["config"],
            "output": str(Path(args.output) / "sweep_summary.json"),
        }
    )
    return 0 if summary["status_counts"].get("error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
