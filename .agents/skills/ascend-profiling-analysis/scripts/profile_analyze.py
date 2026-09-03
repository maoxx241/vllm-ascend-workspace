#!/usr/bin/env python3
"""Run the Ascend profiling analysis pipeline against a single profiling root.

Inputs (one of):
  --manifest <local-run-dir>/manifest.json    -- produced by ascend-profiling-collection
  --remote-profile-root <abs-path>            -- raw remote profiling root (historical)

Behavior:
  1. Resolve the session + SSH endpoint (explicit --session-id/--session-file,
     the manifest's recorded session, or the bound session of the cwd worktree).
  2. Tar-sync ``scripts/ascend_profile/`` to ``<remote-work-dir>/ascend_profile/``.
  3. Remote: ``python3 -m ascend_profile.analyze <ROOT> --output <OUT> --verbose``.
  4. Validate required artifacts exist on the remote.
  5. Pull lightweight artifacts (and report/) back to the local run dir.
  6. Emit a single JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import shutil
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Sequence

try:
    from . import _common as common  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _common as common  # type: ignore[no-redef]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--session-id", help="VAWS session id; defaults to the bound session of the current worktree")
    parser.add_argument("--session-file", help="explicit session.json path")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", help="path to ascend-profiling-collection manifest.json")
    src.add_argument("--remote-profile-root", help="absolute remote path to profiling root")
    parser.add_argument("--tag", default="", help="optional run tag (used in run dir name)")
    parser.add_argument(
        "--remote-work-dir",
        default=common.DEFAULT_REMOTE_WORK_DIR,
        help=f"remote scratch dir for tools + outputs (default: {common.DEFAULT_REMOTE_WORK_DIR})",
    )
    parser.add_argument(
        "--remote-output-dir",
        default=None,
        help=(
            "explicit remote output directory (absolute path). Useful with "
            "--from-stage / --only-stage to reuse a prior run's artifacts; "
            "default: <remote-work-dir>/runs/<local-run-dir-name>."
        ),
    )
    parser.add_argument(
        "--local-output-dir",
        default=None,
        help=(
            "explicit local directory to write pulled artifacts into. "
            "Default: .vaws-local/profiling-analysis/runs/<timestamp>_<tag>/. "
            "Existing non-empty directories are rejected unless --overwrite is given."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow --local-output-dir to point at an existing non-empty directory",
    )
    parser.add_argument(
        "--keep-remote-output",
        action="store_true",
        help="pull every file in the remote output dir back to the local run dir",
    )
    parser.add_argument(
        "--remote-timeout",
        type=int,
        default=3600,
        help="hard timeout (seconds) for the remote analyze command",
    )
    parser.add_argument(
        "--skip-html",
        action="store_true",
        help="forward to remote analyze: skip HTML rendering entirely",
    )
    parser.add_argument(
        "--report-mode",
        choices=("summary", "full-raw"),
        default="full-raw",
        help=(
            "forward to remote analyze: 'summary' (md+xlsx only, HTML is "
            "a stub) for first-stage pipeline debugging; 'full-raw' "
            "(default) renders the complete L1/L2/L3 HTML with operator "
            "cards backed by raw kernel_details rows."
        ),
    )
    parser.add_argument("--model-id", help="optional model id/name for report context")
    parser.add_argument(
        "--model-config",
        help=(
            "optional config.json for comparison. If the path exists locally, "
            "the wrapper uploads it into this run's remote output dir; "
            "otherwise it is treated as a remote path."
        ),
    )
    parser.add_argument("--hardware-model", help="optional capture hardware model, e.g. Ascend910B4")
    parser.add_argument(
        "--hardware-profile",
        help=(
            "optional hardware_profile.json. If the path exists locally, the "
            "wrapper uploads it; otherwise it is treated as a remote path."
        ),
    )
    parser.add_argument(
        "--no-cann-hardware-scan",
        action="store_true",
        help="disable remote CANN platform_config scanning",
    )
    parser.add_argument(
        "--from-stage",
        choices=("normalize", "segment", "classify", "summarize", "cross_rank", "diagnostics", "report"),
        help="forward to remote analyze: resume from this stage (skip earlier ones)",
    )
    parser.add_argument(
        "--to-stage",
        choices=("normalize", "segment", "classify", "summarize", "cross_rank", "diagnostics", "report"),
        help="forward to remote analyze: stop after this stage",
    )
    parser.add_argument(
        "--only-stage",
        choices=("normalize", "segment", "classify", "summarize", "cross_rank", "diagnostics", "report"),
        help="forward to remote analyze: run exactly one stage (e.g. report)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def _manifest_default_hardware_model(manifest: dict[str, Any] | None) -> str | None:
    if not manifest:
        return None
    for key in ("hardware_model", "npu_name", "device_name", "chip_name", "soc_version"):
        value = manifest.get(key)
        if value:
            return str(value)
    snapshot = manifest.get("hardware_snapshot")
    if isinstance(snapshot, dict):
        for key in ("hardware_model", "npu_name", "device_name", "chip_name", "soc_version"):
            value = snapshot.get(key)
            if value:
                return str(value)
        devices = snapshot.get("devices")
        if isinstance(devices, list) and devices:
            first = devices[0] if isinstance(devices[0], dict) else {}
            for key in ("name", "hardware_model", "chip_name", "soc_version"):
                value = first.get(key)
                if value:
                    return str(value)
    return None


def _maybe_upload_local_file(
    endpoint: common.SshEndpoint,
    run_dir: Path,
    local_or_remote: str | None,
    remote_output_dir: str,
    *,
    upload_subdir: str,
) -> str | None:
    if not local_or_remote:
        return None
    path = Path(local_or_remote).expanduser()
    if not path.is_file():
        return local_or_remote
    upload_dir = run_dir / upload_subdir
    if upload_dir.exists():
        shutil.rmtree(upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dst = upload_dir / path.name
    shutil.copy2(path, dst)
    remote_dir = f"{remote_output_dir.rstrip('/')}/{upload_subdir}"
    common.sync_to_remote(endpoint, upload_dir, remote_dir)
    return f"{remote_dir}/{path.name}"


def _resolve_input(args: argparse.Namespace) -> dict[str, Any]:
    """Return ``{"remote_profile_root": str, "manifest": dict | None}``.

    Hard-fails on incomplete collection manifests.
    """
    if args.manifest:
        manifest_path = Path(args.manifest).expanduser().resolve()
        manifest = common.load_collection_manifest(manifest_path)
        return {
            "remote_profile_root": manifest["remote_profile_root"],
            "manifest": manifest,
            "manifest_path": str(manifest_path),
        }
    return {
        "remote_profile_root": args.remote_profile_root,
        "manifest": None,
        "manifest_path": None,
    }


def _resolve_end_stage(
    only_stage: str | None,
    from_stage: str | None,
    to_stage: str | None,
) -> str:
    """Mirror ``ascend_profile.analyze._resolve_stage_window`` but lighter:
    we only need the *end* stage to pick the required-artifacts set.
    """
    if only_stage:
        return only_stage
    if to_stage:
        return to_stage
    # No explicit window means the full pipeline; the wrapper validates the
    # full ``report`` artifact set.
    return "report"


def _required_artifacts_for(end_stage: str) -> tuple[str, ...]:
    return common.REQUIRED_ARTIFACTS_BY_END_STAGE.get(
        end_stage, common.REQUIRED_SINGLE_ARTIFACTS
    )


def _validate_remote_artifacts(
    endpoint: common.SshEndpoint,
    remote_output_dir: str,
    *,
    required_artifacts: tuple[str, ...] = common.REQUIRED_SINGLE_ARTIFACTS,
) -> dict[str, Any]:
    """Confirm required artifacts exist; raise on missing files.

    ``required_artifacts`` is scoped to the stage window the wrapper just
    asked for, so partial reruns (``--only-stage normalize``) don't get
    flagged for not producing ``report/report.md``.
    """
    quoted = common.quote_remote(remote_output_dir)
    listing = common.ssh_exec(
        endpoint,
        "set -e; "
        f"cd {quoted} && "
        "for f in "
        + " ".join(common.quote_remote(p) for p in required_artifacts)
        + "; do test -f \"$f\" && echo OK:\"$f\" || echo MISSING:\"$f\"; done",
        check=True,
        timeout=120,
    )
    missing = [
        line.split(":", 1)[1]
        for line in listing.stdout.splitlines()
        if line.startswith("MISSING:")
    ]
    if missing:
        raise RuntimeError(
            f"required artifacts missing in {remote_output_dir}: {missing}"
        )

    cat = common.ssh_exec(
        endpoint,
        f"cat {common.quote_remote(remote_output_dir + '/manifest.json')}",
        check=True,
        timeout=60,
    )
    try:
        return json.loads(cat.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"remote manifest.json is not valid JSON at {remote_output_dir}: {e}"
        ) from e


def _validate_segment_health(endpoint: common.SshEndpoint, remote_output_dir: str) -> dict[str, Any]:
    """Surface segmentation hard errors / interior islands as failures.

    The framework already emits these in ``segment_manifest.json``; we just
    refuse to declare success when they are non-zero. Returns a health summary
    including per-rank segmentation strategies so a knowledge-base miss
    (``exact_cover_knowledge_miss``) is visible at the top level instead of
    being buried in the manifest.
    """
    cat = common.ssh_exec(
        endpoint,
        f"cat {common.quote_remote(remote_output_dir + '/segment_manifest.json')}",
        check=True,
        timeout=60,
    )
    try:
        seg = json.loads(cat.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"segment_manifest.json is not valid JSON: {e}") from e

    # New schema: ``hard_error_count`` (int) + ``interior_island_total`` (int) +
    # ``hard_errors`` (list).  Older drafts emitted only ``hard_errors`` as a
    # list, so accept both.
    raw_hard = seg.get("hard_error_count")
    if raw_hard is None:
        legacy_hard = seg.get("hard_errors", 0)
        if isinstance(legacy_hard, list):
            raw_hard = len(legacy_hard)
        else:
            raw_hard = legacy_hard
    hard = int(raw_hard or 0)

    interior = int(seg.get("interior_island_total", 0) or 0)
    if interior == 0:
        for rank in seg.get("rank_summaries", []) or []:
            interior += int(rank.get("interior_unclassified_count") or 0)

    if hard or interior:
        raise RuntimeError(
            "segmentation reported unrecoverable issues "
            f"(hard_error_count={hard}, interior_island_total={interior}); "
            "see segment_manifest.json for details"
        )

    strategy_modes: dict[str, str] = {}
    for rank in seg.get("rank_summaries", []) or []:
        strategy = rank.get("segmentation_strategy") or {}
        strategy_modes[str(rank.get("rank_id"))] = str(strategy.get("mode") or "unknown")
    degraded_ranks = sorted(
        rank_id for rank_id, mode in strategy_modes.items() if mode == "exact_cover_knowledge_miss"
    )
    return {
        "strategy_modes": strategy_modes,
        "degraded_ranks": degraded_ranks,
    }


def _diagnosis_counts(local_run_dir: Path) -> dict[str, int]:
    """Aggregate findings by confidence level.

    The diagnosis stage emits findings under the ``diagnosis_findings`` key
    (schema: scripts/ascend_profile/diagnostics.py). Older drafts used
    ``findings`` / ``claims``; we keep those as fallbacks so the skill
    survives a schema rename.
    """
    findings_path = local_run_dir / "diagnosis_findings.json"
    if not findings_path.is_file():
        return {}
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    findings = (
        data.get("diagnosis_findings")
        or data.get("findings")
        or data.get("claims")
        or []
    )
    counts: dict[str, int] = {}
    for finding in findings:
        confidence = str(finding.get("confidence", "unknown"))
        counts[confidence] = counts.get(confidence, 0) + 1
    return counts


def _write_local_run_meta(
    run_dir: Path,
    *,
    machine: str,
    remote_profile_root: str,
    remote_output_dir: str,
    manifest_path: str | None,
    stage_timings: list[dict[str, Any]],
    elapsed_s: float,
    analysis_context: dict[str, Any],
) -> None:
    meta = {
        "schema_version": 1,
        "tool": "ascend-profiling-analysis",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": machine,
        "remote_profile_root": remote_profile_root,
        "remote_output_dir": remote_output_dir,
        "collection_manifest": manifest_path,
        "stage_timings": stage_timings,
        "elapsed_s": round(elapsed_s, 6),
        "analysis_context": analysis_context,
    }
    (run_dir / "skill_run.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    started = time.time()

    try:
        input_info = _resolve_input(args)
    except (FileNotFoundError, RuntimeError) as exc:
        return common.fail_return("manifest_validation", exc)

    remote_profile_root = input_info["remote_profile_root"]
    manifest = input_info["manifest"]
    if manifest is not None and not args.session_id and not args.session_file:
        args.session_id = manifest.get("session_id")
        args.session_file = manifest.get("session_file")
    if manifest is not None and not args.hardware_model:
        args.hardware_model = _manifest_default_hardware_model(manifest)

    target, fail = common.resolve_wrapper_target(
        session_id=args.session_id,
        session_file=args.session_file,
    )
    if fail is not None:
        return fail
    assert target is not None
    alias = target["alias"]
    endpoint = target["endpoint"]
    common.progress(
        "resolve",
        "target resolved",
        machine=alias,
        mode=target["mode"],
        session_id=target["session_id"],
        host=endpoint.host,
        ssh_port=endpoint.port,
    )

    py, fail = common.require_remote_python(
        endpoint, alias=alias, session_id=target["session_id"]
    )
    if fail is not None:
        return fail

    run_dir, fail = common.prepare_run_dir(
        args.tag,
        explicit_dir=args.local_output_dir,
        overwrite=args.overwrite,
        alias=alias,
        session_id=target["session_id"],
    )
    if fail is not None:
        return fail
    assert run_dir is not None

    if manifest is not None:
        (run_dir / "collection_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    remote_work_dir = args.remote_work_dir.rstrip("/")
    if args.remote_output_dir:
        remote_output_dir = args.remote_output_dir
    else:
        remote_output_dir = f"{remote_work_dir}/runs/{run_dir.name}"

    # Phase 1: parity sync (only scripts/ascend_profile/)
    try:
        common.sync_framework(endpoint, remote_work_dir, remote_output_dir)
        remote_model_config = _maybe_upload_local_file(
            endpoint,
            run_dir,
            args.model_config,
            remote_output_dir,
            upload_subdir="input_model_config",
        )
        remote_hardware_profile = _maybe_upload_local_file(
            endpoint,
            run_dir,
            args.hardware_profile,
            remote_output_dir,
            upload_subdir="input_hardware_profile",
        )
    except (RuntimeError, FileNotFoundError) as exc:
        return common.fail_return(
            "parity_sync", exc, machine=alias, remote_profile_root=remote_profile_root
        )

    # Phase 2: remote analyze
    extra_flags: list[str] = []
    if args.verbose:
        extra_flags.append("--verbose")
    if args.skip_html:
        extra_flags.append("--skip-html")
    extra_flags.extend(["--report-mode", args.report_mode])
    if args.from_stage:
        extra_flags.extend(["--from-stage", args.from_stage])
    if args.to_stage:
        extra_flags.extend(["--to-stage", args.to_stage])
    if args.only_stage:
        extra_flags.extend(["--only-stage", args.only_stage])
    if args.model_id:
        extra_flags.extend(["--model-id", args.model_id])
    if remote_model_config:
        extra_flags.extend(["--model-config", remote_model_config])
    if args.hardware_model:
        extra_flags.extend(["--hardware-model", args.hardware_model])
    if remote_hardware_profile:
        extra_flags.extend(["--hardware-profile", remote_hardware_profile])
    if args.no_cann_hardware_scan:
        extra_flags.append("--no-cann-hardware-scan")
    cmd = (
        f"set -e; cd {common.quote_remote(remote_work_dir)} && "
        f"{py} -m {common.FRAMEWORK_PYTHON_MODULE}.analyze "
        f"{common.quote_remote(remote_profile_root)} "
        f"--output {common.quote_remote(remote_output_dir)} "
        + " ".join(common.quote_remote(item) for item in extra_flags)
    )
    common.progress(
        "analyze",
        "running remote pipeline",
        remote_profile_root=remote_profile_root,
        remote_output_dir=remote_output_dir,
    )
    rc, fail = common.stream_remote_command(
        endpoint,
        cmd,
        forward_prefix="[ascend_profile] ",
        timeout=args.remote_timeout,
        fail_phase="remote_analyze",
        machine=alias,
        remote_profile_root=remote_profile_root,
        remote_output_dir=remote_output_dir,
    )
    if fail is not None:
        return fail
    if rc != 0:
        return common.fail_return(
            "remote_analyze",
            f"remote analyze exited with rc={rc}",
            machine=alias,
            remote_profile_root=remote_profile_root,
            remote_output_dir=remote_output_dir,
        )

    # Phase 3: validate artifacts and segmentation health.
    #
    # When the caller restricted the stage window (``--only-stage`` /
    # ``--to-stage``), the wrapper only checks the artifact set that
    # *should* exist after that stage. Segment health is re-validated
    # whenever ``segment_manifest.json`` is part of the expected set.
    end_stage = _resolve_end_stage(args.only_stage, args.from_stage, args.to_stage)
    required_artifacts = _required_artifacts_for(end_stage)
    try:
        remote_manifest = _validate_remote_artifacts(
            endpoint, remote_output_dir, required_artifacts=required_artifacts
        )
        segment_health: dict[str, Any] = {}
        if "segment_manifest.json" in required_artifacts:
            segment_health = _validate_segment_health(endpoint, remote_output_dir)
    except RuntimeError as exc:
        return common.fail_return(
            "artifact_validation",
            exc,
            machine=alias,
            remote_profile_root=remote_profile_root,
            remote_output_dir=remote_output_dir,
        )

    # Phase 4: pull artifacts back
    try:
        common.pull_artifacts(
            endpoint,
            remote_output_dir,
            run_dir,
            keep_remote_output=args.keep_remote_output,
            include_paths=common.LIGHTWEIGHT_PULL_PATHS,
        )
    except RuntimeError as exc:
        return common.fail_return(
            "artifact_pull",
            exc,
            machine=alias,
            remote_profile_root=remote_profile_root,
            remote_output_dir=remote_output_dir,
        )

    elapsed = time.time() - started
    stage_timings = remote_manifest.get("stage_timings", [])
    analysis_context = remote_manifest.get("analysis_context", {}) or {}
    _write_local_run_meta(
        run_dir,
        machine=alias,
        remote_profile_root=remote_profile_root,
        remote_output_dir=remote_output_dir,
        manifest_path=input_info.get("manifest_path"),
        stage_timings=stage_timings,
        elapsed_s=elapsed,
        analysis_context=analysis_context,
    )

    stage_results = remote_manifest.get("stage_results", {}) or {}
    normalize_info = stage_results.get("normalize", {}) or {}
    segment_info = stage_results.get("segment", {}) or {}

    report_manifest_path = run_dir / "report" / "manifest.json"
    html_status = "unknown"
    if report_manifest_path.is_file():
        try:
            html_status = json.loads(report_manifest_path.read_text(encoding="utf-8")).get("html_status", "unknown")
        except (json.JSONDecodeError, OSError):
            html_status = "unknown"

    # A knowledge-base miss falls back to exact-cover search; results are still
    # produced but structure attribution is weaker, so surface it prominently
    # instead of returning an indistinguishable clean "ok".
    degraded_ranks = segment_health.get("degraded_ranks") or []
    warnings: list[str] = []
    if degraded_ranks:
        warnings.append(
            f"segmentation knowledge base did not match ranks {degraded_ranks}; "
            "fell back to exact-cover search (weaker structure attribution). "
            "Consider extending kernel_signatures.yaml for this model."
        )

    output: dict[str, Any] = {
        "status": "ok",
        "segmentation_degraded": bool(degraded_ranks),
        "warnings": warnings,
        "segmentation_strategies": segment_health.get("strategy_modes") or {},
        "machine": alias,
        "mode": target["mode"],
        "session_id": target["session_id"],
        "session_file": target["session_file"],
        "remote_profile_root": remote_profile_root,
        "remote_output_dir": remote_output_dir,
        "local_output_dir": str(run_dir),
        "stage_timings": stage_timings,
        "rank_count": normalize_info.get("rank_count"),
        "event_count": normalize_info.get("event_count"),
        "segment_count": segment_info.get("segment_count"),
        "layer_count": segment_info.get("layer_count"),
        "diagnosis_counts": _diagnosis_counts(run_dir),
        "report_md": str(run_dir / "report" / "report.md"),
        "report_xlsx": str(run_dir / "report" / "report.xlsx"),
        "report_html": str(run_dir / "report" / "report.html"),
        "html_status": html_status,
        "analysis_context": analysis_context,
        "elapsed_s": round(elapsed, 6),
    }
    common.print_json(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
