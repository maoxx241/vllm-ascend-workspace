#!/usr/bin/env python3
"""Run torch_npu.profiler.profiler.analyse(...) on the remote container.

vLLM's torch profiler integration writes raw ``*_ascend_pt`` directories under
the configured ``torch_profiler_dir``. They must be post-processed by
``torch_npu.profiler.profiler.analyse(...)`` to materialize the
``ASCEND_PROFILER_OUTPUT/`` files (``kernel_details.csv``,
``trace_view.json``, ...). This script wraps that single call with a
shell-safe Ascend env preamble.

The agent always passes ``--profile-root`` (the directory that contains one or
more ``*_ascend_pt`` subdirectories, typically
``<runtime_dir>/<torch_profiler_dir>``). Every matching subdirectory is
analysed in sorted order, then verified -- ``profiling-inventory.md``
documents several captures where ``analyse`` "succeeded" but produced no
``kernel_details.csv`` (short capture window, missing FRAMEWORK data), so
verification turns that failure mode into a hard exit instead of letting
downstream analysis silently process degenerate roots.

Exit codes:
    0  -- every rank produced kernel_details.csv and trace_view.json AND
          (when --expected-ranks is given) the rank count matches
    1  -- at least one rank is incomplete OR the rank count does not match
          (missing_kernel_details / rank_count_mismatch / partial)
    2  -- the SSH or analyse() call itself failed

Usage:
    python3 run_remote_analyse.py --machine <alias> \\
        --profile-root <path> [--expected-ranks <N>]

The agent should always pass ``--expected-ranks`` when invoking this from a
collection orchestrator (typically ``tp * (dp or 1)``); otherwise a partial
capture where some ranks never produced a ``*_ascend_pt`` directory will look
"clean" because every directory that *did* land was complete.

Progress on stderr, final JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    ASCEND_ENV_PREAMBLE,
    container_endpoint,
    emit_progress,
    print_json,
    resolve_machine,
    ssh_exec,
)

EXPECTED_OUTPUTS = {
    "kernel_details_csv": "ASCEND_PROFILER_OUTPUT/kernel_details.csv",
    "trace_view_json": "ASCEND_PROFILER_OUTPUT/trace_view.json",
}


def list_ascend_pt_dirs(ep, profile_root: str) -> list[str]:
    """Return sorted ``*_ascend_pt`` directories directly under profile_root."""
    cmd = (
        f"find {shlex.quote(profile_root)} -maxdepth 1 -type d "
        "-name '*_ascend_pt' | sort"
    )
    result = ssh_exec(ep, cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to list profile dirs under {profile_root}: "
            f"{result.stderr[:1000]}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def run_analyse(ep, remote_dir: str) -> None:
    """Run torch_npu.profiler.profiler.analyse(remote_dir) on the container."""
    py = (
        "from torch_npu.profiler.profiler import analyse\n"
        f"analyse({json.dumps(remote_dir)})\n"
    )
    script = f"{ASCEND_ENV_PREAMBLE}\npython3 -c {shlex.quote(py)}\n"
    result = ssh_exec(ep, script, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"remote analyse({remote_dir!r}) failed (rc={result.returncode}):\n"
            f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
        )


def verify_outputs(ep, remote_dir: str) -> dict[str, Any]:
    """Check that the expected ASCEND_PROFILER_OUTPUT files exist."""
    outputs: dict[str, Any] = {}
    for key, rel in EXPECTED_OUTPUTS.items():
        path = f"{remote_dir.rstrip('/')}/{rel}"
        result = ssh_exec(ep, f"test -f {shlex.quote(path)}", check=False)
        outputs[key] = {
            "path": path,
            "exists": result.returncode == 0,
        }
    return outputs


def classify_status(outputs: dict[str, Any]) -> str:
    """Map output presence to an ``analysis_status`` value.

    - ``ok``: every expected output present
    - ``missing_kernel_details``: kernel_details.csv missing (the canonical
      "analyse ran but device data did not land" case from
      ``profiling-inventory.md``)
    - ``partial``: some other expected file is missing
    """
    if not outputs["kernel_details_csv"]["exists"]:
        return "missing_kernel_details"
    if not all(v["exists"] for v in outputs.values()):
        return "partial"
    return "ok"


def analyse_profile_root(
    ep,
    profile_root: str,
    *,
    expected_ranks: int | None = None,
) -> dict[str, Any]:
    """Discover, analyse, and verify every *_ascend_pt under profile_root.

    When ``expected_ranks`` is provided, the rank count is enforced: missing
    ranks land as ``analysis_status = "rank_count_mismatch"`` even if every
    directory that *did* exist was complete. This is the canonical "rank N
    silently failed to dump anything" failure mode.

    Returns a dict ready to merge into the collection manifest:

        {
          "profile_root": "...",
          "expected_ranks": int | None,
          "rank_count": int,
          "analysis_status": "ok | missing_kernel_details |
                              rank_count_mismatch | partial",
          "dirs": [ {path, outputs, analysis_status}, ... ],
        }
    """
    targets = list_ascend_pt_dirs(ep, profile_root)
    rank_count = len(targets)
    if not targets:
        return {
            "profile_root": profile_root,
            "expected_ranks": expected_ranks,
            "rank_count": 0,
            "analysis_status": "no_profile_dirs",
            "dirs": [],
        }

    analysed: list[dict[str, Any]] = []
    for path in targets:
        emit_progress("analyse", f"analysing {path}")
        run_analyse(ep, path)
        outputs = verify_outputs(ep, path)
        status = classify_status(outputs)
        analysed.append({
            "path": path,
            "outputs": outputs,
            "analysis_status": status,
        })

    # Per-rank classification first: a missing kernel_details.csv on any rank
    # is the most actionable signal and short-circuits the rest.
    per_rank_worst = "ok"
    for item in analysed:
        s = item["analysis_status"]
        if s == "missing_kernel_details":
            per_rank_worst = "missing_kernel_details"
            break
        if s == "partial":
            per_rank_worst = "partial"

    # Worst-of priority: missing_kernel_details > rank_count_mismatch > partial
    # > ok. rank_count_mismatch is reported only when no per-rank csv is
    # missing, otherwise the per-rank failure is a strictly more useful
    # signal (and the count would naturally be off anyway).
    if per_rank_worst == "missing_kernel_details":
        worst = "missing_kernel_details"
    elif expected_ranks is not None and rank_count != expected_ranks:
        worst = "rank_count_mismatch"
    else:
        worst = per_rank_worst

    return {
        "profile_root": profile_root,
        "expected_ranks": expected_ranks,
        "rank_count": rank_count,
        "analysis_status": worst,
        "dirs": analysed,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--machine", required=True, help="machine alias or host IP")
    p.add_argument(
        "--profile-root",
        required=True,
        help=(
            "remote directory containing one or more *_ascend_pt subdirectories "
            "(typically <runtime_dir>/<torch_profiler_dir>)."
        ),
    )
    p.add_argument(
        "--expected-ranks",
        type=int,
        default=None,
        help=(
            "expected number of *_ascend_pt directories (typically "
            "tp * (dp or 1)); when set, a mismatch fails with "
            "analysis_status=rank_count_mismatch even if every present rank "
            "was complete"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        record = resolve_machine(args.machine)
        alias = record["alias"]
        ep = container_endpoint(record)

        emit_progress("discover", f"listing *_ascend_pt under {args.profile_root}")
        bundle = analyse_profile_root(
            ep, args.profile_root, expected_ranks=args.expected_ranks
        )
        bundle["machine"] = alias

        worst = bundle["analysis_status"]
        if worst == "no_profile_dirs":
            bundle["status"] = "failed"
            bundle["error"] = "no *_ascend_pt directories found"
            print_json(bundle)
            return 1

        bundle["status"] = "ok" if worst == "ok" else worst
        print_json(bundle)
        return 0 if worst == "ok" else 1

    except Exception as exc:
        print_json({
            "status": "failed",
            "machine": getattr(args, "machine", None),
            "profile_root": getattr(args, "profile_root", None),
            "error": str(exc),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
