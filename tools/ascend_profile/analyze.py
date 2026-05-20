#!/usr/bin/env python3
"""Run the full Ascend profiling analysis pipeline."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from .classify import classify_profile
    from .common import SCHEMA_VERSION, TOOL_VERSION, read_json, utc_now, write_json
    from .cross_rank import cross_rank_profile
    from .diagnostics import diagnose_profile
    from .normalize import normalize_profile
    from .report import render_report
    from .segment import segment_profile
    from .summarize import summarize_profile
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from classify import classify_profile  # type: ignore[no-redef]
    from common import SCHEMA_VERSION, TOOL_VERSION, read_json, utc_now, write_json  # type: ignore[no-redef]
    from cross_rank import cross_rank_profile  # type: ignore[no-redef]
    from diagnostics import diagnose_profile  # type: ignore[no-redef]
    from normalize import normalize_profile  # type: ignore[no-redef]
    from report import render_report  # type: ignore[no-redef]
    from segment import segment_profile  # type: ignore[no-redef]
    from summarize import summarize_profile  # type: ignore[no-redef]


def run_stage(name: str, func: Callable[[], dict[str, Any]], *, verbose: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    start = time.time()
    if verbose:
        print(f"[ascend_profile] start {name}", flush=True)
    result = func()
    elapsed = time.time() - start
    if verbose:
        print(f"[ascend_profile] done {name} {elapsed:.3f}s", flush=True)
    return result, {"stage": name, "elapsed_s": round(elapsed, 6)}


def analyze_profile(profile_root: Path, output_dir: Path, *, verbose: bool = False) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timings: list[dict[str, Any]] = []
    normalize_result, timing = run_stage("normalize", lambda: normalize_profile(profile_root, output_dir), verbose=verbose)
    timings.append(timing)
    segment_result, timing = run_stage("segment", lambda: segment_profile(output_dir), verbose=verbose)
    timings.append(timing)
    classify_result, timing = run_stage("classify", lambda: classify_profile(output_dir), verbose=verbose)
    timings.append(timing)
    summary_result, timing = run_stage("summarize", lambda: summarize_profile(output_dir), verbose=verbose)
    timings.append(timing)
    cross_rank_result, timing = run_stage("cross_rank", lambda: cross_rank_profile(output_dir), verbose=verbose)
    timings.append(timing)
    diagnosis_result, timing = run_stage("diagnostics", lambda: diagnose_profile(output_dir), verbose=verbose)
    timings.append(timing)
    report_result, timing = run_stage("report", lambda: render_report(output_dir), verbose=verbose)
    timings.append(timing)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "full_pipeline",
        "created_at": utc_now(),
        "profile_root": str(profile_root.resolve()),
        "output_dir": str(output_dir.resolve()),
        "stage_timings": timings,
        "files": {
            "normalize_manifest": "normalize_manifest.json",
            "segment_manifest": "segment_manifest.json",
            "classify_manifest": "classify_manifest.json",
            "summary_manifest": "summary_manifest.json",
            "cross_rank_manifest": "cross_rank_manifest.json",
            "diagnosis_findings": "diagnosis_findings.json",
            "report_manifest": "report/manifest.json",
            "report_md": "report/report.md",
            "report_xlsx": "report/report.xlsx",
        },
        "stage_results": {
            "normalize": normalize_result,
            "segment": segment_result,
            "classify": classify_result,
            "summarize": summary_result,
            "cross_rank": cross_rank_result,
            "diagnostics": {
                "counts": diagnosis_result.get("counts"),
            },
            "report": report_result,
        },
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_root")
    parser.add_argument("--output", required=True)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = analyze_profile(Path(args.profile_root), Path(args.output), verbose=bool(args.verbose))
    print({"stage": "full_pipeline", "output_dir": manifest["output_dir"], "stage_timings": manifest["stage_timings"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

