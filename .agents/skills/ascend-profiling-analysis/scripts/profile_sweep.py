#!/usr/bin/env python3
"""Run the Ascend profiling analysis pipeline against many profiling roots.

This is a thin wrapper around ``tools.ascend_profile.sweep`` (which already
discovers roots and aggregates results). The wrapper handles:
  - inventory lookup
  - parity sync of ``tools/ascend_profile/`` to the remote work dir
  - launching ``sweep`` on the remote
  - pulling back ``sweep_summary.json`` plus per-root ``report/`` and
    ``diagnosis_findings.json`` (skips the bulky normalized event index)
  - emitting a summary JSON on stdout
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

try:
    from . import _common as common  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _common as common  # type: ignore[no-redef]


SWEEP_PER_ROOT_INCLUDES = (
    "manifest.json",
    "segment_manifest.json",
    "classify_manifest.json",
    "summary_manifest.json",
    "cross_rank_manifest.json",
    "diagnosis_findings.json",
    "block_segments.json",
    "class_signatures.json",
    "rank_summary.csv",
    "step_summary.csv",
    "step_anatomy.csv",
    "step_class_summary.csv",
    "layer_summary.csv",
    "layer_class_summary.csv",
    "block_summary.csv",
    "block_class_summary.csv",
    "operator_summary.csv",
    "operator_class_summary.csv",
    "hccl_op_summary.csv",
    "hccl_class_summary.csv",
    "report/manifest.json",
    "report/report.md",
    "report/report.xlsx",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--machine", required=True, help="alias or IP from machine inventory")
    parser.add_argument(
        "--search-root",
        action="append",
        required=True,
        help="absolute remote search root (repeat for multiple roots)",
    )
    parser.add_argument("--tag", default="sweep", help="run tag (used in run dir name)")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of analyzed roots")
    parser.add_argument(
        "--remote-work-dir",
        default=common.DEFAULT_REMOTE_WORK_DIR,
        help=f"remote scratch dir (default: {common.DEFAULT_REMOTE_WORK_DIR})",
    )
    parser.add_argument(
        "--remote-timeout",
        type=int,
        default=14400,
        help="hard timeout (seconds) for the remote sweep command",
    )
    parser.add_argument(
        "--keep-remote-output",
        action="store_true",
        help="pull every per-root file (otherwise only summaries + report/ are pulled)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def _layer_inventory(summary: dict[str, Any]) -> dict[str, int]:
    """Aggregate ``rank_layer_inventory`` tuples across ok roots.

    A tuple like ``(27, 40)`` means "this root has at least one rank with
    27 layers and at least one with 40 layers" -- useful for spotting
    multi-shape or speculative-decode captures.
    """
    counter: dict[str, int] = {}
    for item in summary.get("results", []):
        if item.get("status") != "ok":
            continue
        ranks = item.get("rank_layer_inventory") or {}
        layer_set: set[int] = set()
        for layer_counts in ranks.values():
            for key in layer_counts.keys():
                if key.isdigit():
                    layer_set.add(int(key))
        if not layer_set:
            tup_key = "()"
        else:
            tup_key = "(" + ", ".join(str(v) for v in sorted(layer_set)) + ")"
        counter[tup_key] = counter.get(tup_key, 0) + 1
    return dict(sorted(counter.items(), key=lambda kv: -kv[1]))


def _failed_roots(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "root": item.get("root"),
            "error": item.get("error"),
            "elapsed_s": item.get("elapsed_s"),
        }
        for item in summary.get("results", [])
        if item.get("status") != "ok"
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    started = time.time()

    machine = common.resolve_machine(args.machine)
    alias = common.get_machine_alias(machine)
    endpoint = common.endpoint_from_machine(machine)
    common.progress(
        "resolve",
        "machine resolved",
        machine=alias,
        host=endpoint.host,
        ssh_port=endpoint.port,
    )

    run_dir = common.ensure_run_dir(args.tag)
    common.progress("setup", "local run dir created", path=str(run_dir))

    remote_work_dir = args.remote_work_dir.rstrip("/")
    remote_tools_dir = f"{remote_work_dir}/{common.TOOLS_REMOTE_SUBPATH}"
    remote_output_dir = f"{remote_work_dir}/sweeps/{run_dir.name}"

    # Phase 1: parity sync
    try:
        common.ssh_exec(
            endpoint,
            f"mkdir -p {common.quote_remote(remote_tools_dir)} "
            f"{common.quote_remote(remote_output_dir)}",
            check=True,
            timeout=60,
        )
        common.sync_to_remote(endpoint, common.TOOLS_LOCAL_DIR, remote_tools_dir)
    except (RuntimeError, FileNotFoundError) as exc:
        common.print_json(
            {
                "status": "failed",
                "phase": "parity_sync",
                "error": str(exc),
                "machine": alias,
            }
        )
        return 3

    # Phase 2: remote sweep
    py = common.remote_python_with_module(endpoint, "csv")
    sweep_args = " ".join(
        f"--search-root {common.quote_remote(root)}" for root in args.search_root
    )
    if args.limit is not None:
        sweep_args += f" --limit {int(args.limit)}"
    cmd = (
        f"set -e; cd {common.quote_remote(remote_work_dir)} && "
        f"{py} -m tools.ascend_profile.sweep "
        f"{sweep_args} "
        f"--output {common.quote_remote(remote_output_dir)} "
        f"{'--verbose' if args.verbose else ''}"
    )
    common.progress(
        "sweep",
        "running remote sweep",
        remote_output_dir=remote_output_dir,
        search_roots=args.search_root,
    )
    try:
        rc = common.ssh_stream(
            endpoint,
            cmd,
            forward_prefix="[ascend_profile.sweep] ",
            timeout=args.remote_timeout,
        )
    except TimeoutError as exc:
        common.print_json(
            {
                "status": "failed",
                "phase": "remote_sweep",
                "error": str(exc),
                "machine": alias,
                "remote_output_dir": remote_output_dir,
            }
        )
        return 4
    # ``sweep`` returns rc=1 when any root failed; we still want to download
    # the summary so the agent can report which roots failed. Treat rc != 0
    # as ``status="partial"`` rather than blanket failure.

    # Phase 3: pull summary + per-root lightweight artifacts
    summary_remote = f"{remote_output_dir}/sweep_summary.json"
    try:
        cat = common.ssh_exec(
            endpoint, f"cat {common.quote_remote(summary_remote)}", check=False, timeout=120
        )
        if cat.returncode != 0:
            raise RuntimeError(
                f"sweep_summary.json not found on remote ({summary_remote}); "
                f"sweep likely aborted before writing summary"
            )
        summary = json.loads(cat.stdout)
    except (RuntimeError, json.JSONDecodeError) as exc:
        common.print_json(
            {
                "status": "failed",
                "phase": "summary_pull",
                "error": str(exc),
                "machine": alias,
                "remote_output_dir": remote_output_dir,
            }
        )
        return 5

    summary_local_path = run_dir / "sweep_summary.json"
    summary_local_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    try:
        if args.keep_remote_output:
            common.sync_from_remote(endpoint, remote_output_dir, run_dir)
        else:
            includes: list[str] = ["sweep_summary.json"]
            for item in summary.get("results", []):
                if item.get("status") != "ok":
                    continue
                # ``output_dir`` in sweep results is an absolute path; we only
                # need its basename relative to remote_output_dir.
                out = item.get("output_dir") or ""
                rel = Path(out).name
                if not rel:
                    continue
                for sub in SWEEP_PER_ROOT_INCLUDES:
                    includes.append(f"{rel}/{sub}")
            common.sync_from_remote(
                endpoint, remote_output_dir, run_dir, include_paths=includes
            )
    except RuntimeError as exc:
        common.print_json(
            {
                "status": "failed",
                "phase": "artifact_pull",
                "error": str(exc),
                "machine": alias,
                "remote_output_dir": remote_output_dir,
                "summary_path": str(summary_local_path),
            }
        )
        return 6

    failed = _failed_roots(summary)
    layer_inv = _layer_inventory(summary)
    elapsed = time.time() - started
    output = {
        "status": "ok" if not failed else "partial",
        "machine": alias,
        "search_roots": list(args.search_root),
        "root_count": int(summary.get("root_count", 0)),
        "status_counts": summary.get("status_counts", {}),
        "elapsed_s": round(elapsed, 6),
        "summary_path": str(summary_local_path),
        "layer_inventory": layer_inv,
        "failed_roots": failed,
        "remote_output_dir": remote_output_dir,
        "local_output_dir": str(run_dir),
    }
    common.print_json(output)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
