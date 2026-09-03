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
analysed **in parallel** on the remote container (one SSH call, ``xargs -P``,
bounded by ``--analyse-parallelism``, default 8) -- per-rank analyse() calls
are CPU-bound and the containers have hundreds of cores, so a TP16 capture
would otherwise analyse 16 ranks serially. Each rank runs the exact same
preamble + analyse() command as the historical serial path, with its
stdout/stderr captured in ``<dir>/analyse_parallel.log``. Ranks are then
verified -- ``references/behavior.md``
("Output verification") documents several captures where ``analyse``
"succeeded" but produced no
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
    python3 run_remote_analyse.py [--session-id <id>] \\
        --profile-root <path> [--expected-ranks <N>] \\
        [--analyse-timeout <s>] [--analyse-parallelism <N>]

With no --session-id/--session-file the bound session of the current worktree
is used.

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
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    ASCEND_ENV_PREAMBLE,
    emit_progress,
    print_json,
    resolve_execution_target,
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


def run_analyse(ep, remote_dir: str, *, timeout: float = 1800) -> None:
    """Run torch_npu.profiler.profiler.analyse(remote_dir) on the container.

    analyse() parses every raw trace under the rank dir; multi-rank MoE
    captures routinely exceed the generic 180s ssh_exec default, so this
    call carries its own generous bound.

    This is the serial single-rank form, kept for manual debugging of one
    specific rank dir. ``analyse_profile_root`` uses the parallel driver
    (``run_analyse_parallel``) instead.
    """
    py = (
        "from torch_npu.profiler.profiler import analyse\n"
        f"analyse({json.dumps(remote_dir)})\n"
    )
    script = f"{ASCEND_ENV_PREAMBLE}\npython3 -c {shlex.quote(py)}\n"
    result = ssh_exec(ep, script, check=False, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"remote analyse({remote_dir!r}) failed (rc={result.returncode}):\n"
            f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
        )


# ---------------------------------------------------------------------------
# Parallel per-rank analyse (single SSH call, xargs -P on the container)
# ---------------------------------------------------------------------------

# The parallel worker takes the rank dir from argv so one shared script body
# serves every rank; the analyse() call itself is identical to run_analyse.
ANALYSE_PY = (
    "import sys\n"
    "from torch_npu.profiler.profiler import analyse\n"
    "analyse(sys.argv[1])\n"
)

# Sentinels bracketing the per-rank ``rc<TAB>dir`` table on driver stdout.
RESULTS_BEGIN = "__ANALYSE_PARALLEL_RESULTS_BEGIN__"
RESULTS_END = "__ANALYSE_PARALLEL_RESULTS_END__"

# Log file each rank's analyse() stdout/stderr is captured into.
PARALLEL_LOG_NAME = "analyse_parallel.log"


def build_parallel_analyse_script(
    dirs: list[str],
    *,
    parallelism: int,
    timeout_s: float,
    preamble: str = ASCEND_ENV_PREAMBLE,
    py_code: str = ANALYSE_PY,
) -> str:
    """Build the single remote bash script that analyses all dirs concurrently.

    Layout of the generated script:

    - ``analyse_one`` is an exported bash function running *exactly* the
      serial per-rank command (Ascend env preamble + ``analyse()``) inside a
      subshell; the subshell's stdout/stderr land in
      ``<dir>/analyse_parallel.log`` and the per-rank exit code is appended
      to a mktemp'd results file. ``set -e`` from the preamble stays inside
      the subshell, so a failing rank aborts only its own worker.
    - ``printf '%s\\0' <dirs> | timeout --kill-after=10 <T> xargs -0 -P <N>
      -n 1 bash -c 'analyse_one "$1"' _`` fans the ranks out. timeout(1)
      places xargs in its own process group and signals the *group*, so a
      stuck rank cannot leave orphaned worker shells or python processes
      behind; ``--kill-after`` escalates TERM to KILL.
    - After xargs returns, the results table is echoed between
      ``RESULTS_BEGIN`` / ``RESULTS_END`` for the caller to parse, and the
      driver exits with the xargs status (non-zero iff any rank failed or
      the wall timeout fired).

    ``preamble`` / ``py_code`` are injectable so the command shape can be
    exercised locally (see selftest_parallel_analyse.py) without torch_npu.
    """
    if not dirs:
        raise ValueError("dirs must not be empty")
    if parallelism < 1:
        raise ValueError("parallelism must be >= 1")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")
    quoted_dirs = " ".join(shlex.quote(d) for d in dirs)
    preamble_block = "\n".join(
        f"    {line}" if line.strip() else line for line in preamble.splitlines()
    )
    return f"""# Auto-generated parallel analyse driver. Do not hand-edit on the remote.
set -u
PY_CODE={shlex.quote(py_code)}
export PY_CODE
RESULTS="$(mktemp /tmp/analyse_parallel_results.XXXXXX)"
export RESULTS
trap 'rm -f "$RESULTS"' EXIT

analyse_one() {{
  dir="$1"
  log="${{dir%/}}/{PARALLEL_LOG_NAME}"
  (
{preamble_block}
    python3 -c "$PY_CODE" "$dir"
  ) >"$log" 2>&1
  rc=$?
  printf '%s\\t%s\\n' "$rc" "$dir" >>"$RESULTS"
  return "$rc"
}}
export -f analyse_one

printf '%s\\0' {quoted_dirs} | \\
  timeout --kill-after=10 {timeout_s:g} \\
    xargs -0 -P {parallelism} -n 1 bash -c 'analyse_one "$1"' _
xargs_rc=$?

echo "{RESULTS_BEGIN}"
cat "$RESULTS"
echo "{RESULTS_END}"
exit "$xargs_rc"
"""


def parse_parallel_results(stdout: str) -> dict[str, int] | None:
    """Extract the per-rank ``rc<TAB>dir`` table from driver stdout.

    Returns ``{dir: rc}`` or None when the sentinel block is absent (driver
    died before the summary, e.g. SSH drop or local ssh_exec timeout).
    Unparseable lines are skipped so partial tables stay usable.
    """
    begin = stdout.find(RESULTS_BEGIN)
    end = stdout.find(RESULTS_END)
    if begin == -1 or end == -1 or end < begin:
        return None
    block = stdout[begin + len(RESULTS_BEGIN):end]
    results: dict[str, int] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        rc_str, _, path = line.partition("\t")
        if not path:
            continue
        try:
            results[path] = int(rc_str)
        except ValueError:
            continue
    return results


def _tail_rank_logs(ep, dirs: list[str], *, lines: int = 60) -> str:
    """Tail the analyse_parallel.log of the given rank dirs in one SSH call."""
    parts = []
    for d in dirs:
        log = f"{d.rstrip('/')}/{PARALLEL_LOG_NAME}"
        parts.append(
            f"echo '===== {log} ====='; tail -n {lines} {shlex.quote(log)} 2>&1"
        )
    result = ssh_exec(ep, "; ".join(parts), check=False, timeout=60)
    return result.stdout[-3000:]


def run_analyse_parallel(
    ep,
    dirs: list[str],
    *,
    parallelism: int,
    timeout: float = 1800,
    ssh_grace_s: float = 180,
) -> float:
    """Analyse every rank dir concurrently on the remote; return wall seconds.

    Raises RuntimeError -- same "the analyse() call itself failed" semantics
    as the serial path (top-level exit code 2) -- when any rank exits
    non-zero, when the xargs phase hits the ``timeout`` wall (ranks without
    an exit-code line are reported as timed out), or when the SSH transport
    fails. The error message carries the per-rank exit-code table plus tails
    of the failing ranks' ``analyse_parallel.log`` files.

    The local ssh_exec timeout is ``timeout + ssh_grace_s``: the remote
    timeout(1) wrapper is the primary bound and normally fires first with a
    clean 124; the local bound is only the backstop for a dead transport.
    """
    script = build_parallel_analyse_script(
        dirs, parallelism=parallelism, timeout_s=timeout,
    )
    start = time.monotonic()
    result = ssh_exec(ep, script, check=False, timeout=timeout + ssh_grace_s)
    wall_s = time.monotonic() - start

    per_rank = parse_parallel_results(result.stdout)
    if (
        result.returncode == 0
        and per_rank is not None
        and all(d in per_rank for d in dirs)
    ):
        return wall_s

    lines = [
        f"parallel remote analyse failed (rc={result.returncode}, "
        f"parallelism={parallelism}, wall={wall_s:.1f}s, "
        f"wall_timeout={timeout:g}s):",
    ]
    if per_rank is None:
        lines.append("no per-rank results block in driver stdout")
    else:
        for d in dirs:
            rc = per_rank.get(d)
            lines.append(
                f"  rc={rc if rc is not None else 'NO-RESULT (timeout)'}  {d}"
            )
    failing = [d for d in dirs if per_rank is None or per_rank.get(d) != 0]
    if failing:
        lines.append("failing-rank log tails:")
        lines.append(_tail_rank_logs(ep, failing))
    if result.stderr.strip():
        lines.append(f"driver stderr tail: {result.stderr[-1000:]}")
    raise RuntimeError("\n".join(lines))


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
      ``references/behavior.md`` "Output verification")
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
    analyse_timeout: float = 1800,
    analyse_parallelism: int = 8,
) -> dict[str, Any]:
    """Discover, analyse, and verify every *_ascend_pt under profile_root.

    All rank dirs are analysed concurrently on the remote container in one
    SSH call (``run_analyse_parallel``); ``analyse_timeout`` is the overall
    wall-clock bound for that parallel phase, *not* a per-rank budget. The
    effective parallelism is ``min(rank_count, analyse_parallelism)``.

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
          "analyse_wall_s": float,
          "analyse_parallelism": int,
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
            "analyse_wall_s": 0.0,
            "analyse_parallelism": 0,
            "dirs": [],
        }

    parallelism = max(1, min(rank_count, analyse_parallelism))
    emit_progress(
        "analyse",
        f"analysing {rank_count} rank dir(s) under {profile_root} "
        f"(parallelism={parallelism}, wall_timeout={analyse_timeout:g}s)",
    )
    wall_s = run_analyse_parallel(
        ep, targets, parallelism=parallelism, timeout=analyse_timeout,
    )
    emit_progress("analyse", f"parallel analyse finished in {wall_s:.1f}s")

    analysed: list[dict[str, Any]] = []
    for path in targets:
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
        "analyse_wall_s": round(wall_s, 3),
        "analyse_parallelism": parallelism,
        "dirs": analysed,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    target = p.add_mutually_exclusive_group()
    target.add_argument("--session-id", help="VAWS session id; defaults to the bound session of the current worktree")
    target.add_argument("--session-file", help="explicit session.json path")
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
    p.add_argument(
        "--analyse-timeout",
        type=float,
        default=1800,
        help=(
            "overall wall-clock timeout in seconds for the parallel analyse "
            "phase (NOT multiplied by rank count); the remote xargs phase is "
            "wrapped in timeout(1) so stuck ranks are killed"
        ),
    )
    p.add_argument(
        "--analyse-parallelism",
        type=int,
        default=8,
        help=(
            "max concurrent analyse() workers on the remote container; the "
            "effective parallelism is min(rank_count, this value) "
            "(default: 8)"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.analyse_parallelism < 1:
        parser.error("--analyse-parallelism must be >= 1")

    try:
        target = resolve_execution_target(
            session_id=args.session_id,
            session_file=args.session_file,
        )
        alias = target.alias
        ep = target.endpoint

        emit_progress("discover", f"listing *_ascend_pt under {args.profile_root}")
        bundle = analyse_profile_root(
            ep, args.profile_root, expected_ranks=args.expected_ranks,
            analyse_timeout=args.analyse_timeout,
            analyse_parallelism=args.analyse_parallelism,
        )
        bundle["machine"] = alias
        bundle["mode"] = target.mode
        bundle["session_id"] = target.session_id
        bundle["session_file"] = str(target.session_file) if target.session_file else None

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
            "session_id": getattr(args, "session_id", None),
            "session_file": getattr(args, "session_file", None),
            "profile_root": getattr(args, "profile_root", None),
            "error": str(exc),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
