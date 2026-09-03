#!/usr/bin/env python3
"""Run the full Ascend profiling analysis pipeline."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from .classify import classify_profile
    from .common import SCHEMA_VERSION, TOOL_VERSION, emit_stage_json, group_by_rank, read_json, utc_now, write_json
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
    from common import SCHEMA_VERSION, TOOL_VERSION, emit_stage_json, group_by_rank, read_json, utc_now, write_json  # type: ignore[no-redef]
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


REPORT_MODES = ("summary", "full-raw")

# Stage registry. The order is the canonical pipeline order; selectors like
# ``--from-stage`` / ``--to-stage`` / ``--only-stage`` operate on stage names
# from this list. Each entry pairs a stage name with the artifact filename
# the stage produces under ``output_dir`` (used as the resume marker).
STAGE_ORDER = (
    "normalize",
    "segment",
    "classify",
    "summarize",
    "cross_rank",
    "diagnostics",
    "report",
)
STAGE_MARKERS = {
    "normalize": "normalize_manifest.json",
    "segment": "segment_manifest.json",
    "classify": "classify_manifest.json",
    "summarize": "summary_manifest.json",
    "cross_rank": "cross_rank_manifest.json",
    "diagnostics": "diagnosis_findings.json",
    "report": "report/manifest.json",
}


def _resolve_stage_window(
    from_stage: str | None,
    to_stage: str | None,
    only_stage: str | None,
) -> tuple[int, int]:
    """Return inclusive (start_idx, end_idx) into ``STAGE_ORDER``."""
    if only_stage:
        idx = STAGE_ORDER.index(only_stage)
        return idx, idx
    start = STAGE_ORDER.index(from_stage) if from_stage else 0
    end = STAGE_ORDER.index(to_stage) if to_stage else len(STAGE_ORDER) - 1
    if start > end:
        raise ValueError(
            f"--from-stage={from_stage} comes after --to-stage={to_stage}"
        )
    return start, end


def analyze_profile(
    profile_root: Path,
    output_dir: Path,
    *,
    verbose: bool = False,
    skip_html: bool = False,
    report_mode: str = "full-raw",
    from_stage: str | None = None,
    to_stage: str | None = None,
    only_stage: str | None = None,
    model_id: str | None = None,
    model_config: Path | None = None,
    hardware_model: str | None = None,
    hardware_profile: Path | None = None,
    scan_cann_hardware: bool = True,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timings: list[dict[str, Any]] = []
    analysis_context = {
        "model_id": model_id,
        "model_config": str(model_config) if model_config else None,
        "hardware_model": hardware_model,
        "hardware_profile": str(hardware_profile) if hardware_profile else None,
        "scan_cann_hardware": scan_cann_hardware,
    }
    write_json(output_dir / "analysis_context.json", analysis_context)

    start_idx, end_idx = _resolve_stage_window(from_stage, to_stage, only_stage)
    if start_idx > 0:
        # When skipping early stages, require their markers to be present so
        # downstream stages have something to consume.
        missing = []
        for skipped in STAGE_ORDER[:start_idx]:
            marker = output_dir / STAGE_MARKERS[skipped]
            if not marker.exists():
                missing.append(f"{skipped}:{STAGE_MARKERS[skipped]}")
        if missing:
            raise RuntimeError(
                "cannot resume from stage "
                f"'{STAGE_ORDER[start_idx]}'; missing prerequisite outputs: "
                + ", ".join(missing)
            )

    stage_results: dict[str, Any] = {}

    # In-process event hand-off: normalize materializes the full event list
    # once; downstream stages receive it (plus the rank grouping) instead of
    # re-parsing the normalized index from disk. Resumed runs
    # (``--from-stage`` past normalize) leave both as None and each stage
    # falls back to its on-disk loader.
    pipeline_events: list | None = None
    pipeline_events_by_rank: dict | None = None

    def maybe_run(name: str, runner: Callable[[], dict[str, Any]]) -> None:
        idx = STAGE_ORDER.index(name)
        if idx < start_idx or idx > end_idx:
            return
        result, timing = run_stage(name, runner, verbose=verbose)
        timings.append(timing)
        stage_results[name] = result

    def run_normalize() -> dict[str, Any]:
        nonlocal pipeline_events, pipeline_events_by_rank
        events, manifest = normalize_profile(profile_root, output_dir)
        # Keep the events only when a downstream stage will consume them in
        # this process; otherwise let them be garbage-collected.
        if end_idx > STAGE_ORDER.index("normalize"):
            pipeline_events = events
            pipeline_events_by_rank = group_by_rank(events)
        return manifest

    maybe_run("normalize", run_normalize)
    maybe_run(
        "segment",
        lambda: segment_profile(
            output_dir,
            model_id=model_id,
            model_config=model_config,
            events=pipeline_events,
            events_by_rank=pipeline_events_by_rank,
        ),
    )
    maybe_run(
        "classify",
        lambda: classify_profile(
            output_dir,
            events=pipeline_events,
            events_by_rank=pipeline_events_by_rank,
        ),
    )
    maybe_run(
        "summarize",
        lambda: summarize_profile(
            output_dir,
            model_id=model_id,
            model_config=model_config,
            hardware_model=hardware_model,
            hardware_profile=hardware_profile,
            scan_cann_hardware=scan_cann_hardware,
            events=pipeline_events,
            events_by_rank=pipeline_events_by_rank,
        ),
    )
    maybe_run("cross_rank", lambda: cross_rank_profile(output_dir, events=pipeline_events))
    maybe_run("diagnostics", lambda: diagnose_profile(output_dir))
    maybe_run(
        "report",
        lambda: render_report(
            output_dir,
            skip_html=skip_html,
            report_mode=report_mode,
            events=pipeline_events,
        ),
    )

    # Pull individual variables back so the manifest section below stays
    # readable. Skipped stages report None.
    normalize_result = stage_results.get("normalize", {})
    segment_result = stage_results.get("segment", {})
    classify_result = stage_results.get("classify", {})
    summary_result = stage_results.get("summarize", {})
    cross_rank_result = stage_results.get("cross_rank", {})
    diagnosis_result = stage_results.get("diagnostics", {})
    report_result = stage_results.get("report", {})
    executed = [STAGE_ORDER[i] for i in range(start_idx, end_idx + 1)]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "full_pipeline",
        "created_at": utc_now(),
        "profile_root": str(profile_root.resolve()),
        "output_dir": str(output_dir.resolve()),
        "stage_timings": timings,
        "stages_executed": executed,
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
        "analysis_context": analysis_context,
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
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("profile_root")
    parser.add_argument("--output", required=True)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--skip-html",
        action="store_true",
        help=(
            "skip HTML report rendering entirely. Useful for sweep runs or "
            "minimal CI re-runs that only need report.md / report.xlsx."
        ),
    )
    parser.add_argument(
        "--report-mode",
        choices=list(REPORT_MODES),
        default="full-raw",
        help=(
            "HTML report depth. 'summary' = md+xlsx only (HTML is a stub) — "
            "for first-stage pipeline debugging. 'full-raw' (default) = "
            "complete L1/L2/L3 HTML with operator cards backed by raw "
            "kernel_details rows."
        ),
    )
    parser.add_argument("--model-id", help="optional user-supplied model id/name for report context")
    parser.add_argument("--model-config", help="optional config.json path available on this analysis host")
    parser.add_argument("--hardware-model", help="optional capture hardware model, e.g. Ascend910B4")
    parser.add_argument("--hardware-profile", help="optional hardware_profile.json path available on this analysis host")
    parser.add_argument(
        "--no-cann-hardware-scan",
        action="store_true",
        help="disable CANN platform_config scanning for hardware theoretical peaks",
    )
    stage_group = parser.add_argument_group("stage selection (advanced)")
    stage_group.add_argument(
        "--from-stage",
        choices=list(STAGE_ORDER),
        help=(
            "skip stages strictly before this one, reusing artifacts already "
            "on disk. Requires every prior stage's marker file to exist."
        ),
    )
    stage_group.add_argument(
        "--to-stage",
        choices=list(STAGE_ORDER),
        help="stop after this stage finishes.",
    )
    stage_group.add_argument(
        "--only-stage",
        choices=list(STAGE_ORDER),
        help=(
            "run exactly one stage (e.g. `--only-stage report` to re-render "
            "after editing report.py). Implies --from-stage and --to-stage."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = analyze_profile(
        Path(args.profile_root),
        Path(args.output),
        verbose=bool(args.verbose),
        skip_html=bool(args.skip_html),
        report_mode=args.report_mode,
        from_stage=args.from_stage,
        to_stage=args.to_stage,
        only_stage=args.only_stage,
        model_id=args.model_id,
        model_config=Path(args.model_config) if args.model_config else None,
        hardware_model=args.hardware_model,
        hardware_profile=Path(args.hardware_profile) if args.hardware_profile else None,
        scan_cann_hardware=not bool(args.no_cann_hardware_scan),
    )
    emit_stage_json({
        "stage": "full_pipeline",
        "output_dir": manifest["output_dir"],
        "stages_executed": manifest.get("stages_executed"),
        "stage_timings": manifest["stage_timings"],
        "skip_html": bool(args.skip_html),
        "report_mode": args.report_mode,
        "model_id": args.model_id,
        "hardware_model": args.hardware_model,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
