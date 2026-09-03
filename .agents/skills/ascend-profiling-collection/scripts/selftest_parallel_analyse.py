#!/usr/bin/env python3
"""Local self-test for the parallel analyse driver (no remote, no torch_npu).

``build_parallel_analyse_script`` is a pure function, so the exact bash the
remote container would run can be executed locally against fake
``*_ascend_pt`` directories with a stub python payload. Validates:

1. command shape -- one ``timeout --kill-after``-wrapped ``xargs -P``
   pipeline, generated script parses under ``bash -n``;
2. per-rank logs -- every rank's stdout/stderr lands in
   ``<dir>/analyse_parallel.log``;
3. exit-code aggregation -- ``parse_parallel_results`` recovers every
   per-rank rc; any non-zero rank makes the driver exit non-zero;
4. timeout behaviour (only when a real GNU timeout(1) is available) -- a
   rank stuck past ``timeout_s`` yields rc 124 and no result line for that
   rank while the other ranks still complete.

The real ``ASCEND_ENV_PREAMBLE`` is used unmodified; its
``[ -f /etc/profile.d/vaws-ascend-env.sh ]`` guard is a no-op off-container.

Run: python3 scripts/selftest_parallel_analyse.py
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from run_remote_analyse import (  # noqa: E402
    PARALLEL_LOG_NAME,
    RESULTS_BEGIN,
    RESULTS_END,
    build_parallel_analyse_script,
    parse_parallel_results,
)

STUB_PY = (
    "import sys\n"
    "print('stub analyse of', sys.argv[1])\n"
    "sys.exit(7 if 'fail' in sys.argv[1] else 0)\n"
)

SLEEPY_PY = (
    "import sys, time\n"
    "if 'slow' in sys.argv[1]:\n"
    "    time.sleep(30)\n"
    "print('stub analyse of', sys.argv[1])\n"
)

_FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _FAILURES.append(label)


def make_rank_dirs(root: Path, names: list[str]) -> list[str]:
    dirs = []
    for name in names:
        d = root / name
        d.mkdir(parents=True)
        dirs.append(str(d))
    return dirs


def run_script(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, timeout=120,
    )


def install_timeout_shim(tmp: Path, env: dict[str, str]) -> bool:
    """macOS lacks GNU timeout(1); install a passthrough shim on PATH.

    Returns True when a real timeout(1) was found (no shim installed).
    """
    if shutil.which("timeout"):
        return True
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "timeout"
    shim.write_text(
        "#!/bin/bash\n"
        "# passthrough shim: drop --kill-after=X style options, then the\n"
        "# duration, then exec the command (no real timeout enforcement)\n"
        'while [[ "$1" == --* ]]; do shift; done\n'
        "shift\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="analyse_parallel_selftest_"))
    env = dict(os.environ)
    real_timeout = install_timeout_shim(tmp, env)
    print(f"selftest workspace: {tmp}")
    print(f"timeout(1): {'real' if real_timeout else 'shimmed (no-op)'}")

    # -- Case 0: generated script is syntactically valid bash ---------------
    dirs0 = make_rank_dirs(tmp / "case_shape", ["rank0_ascend_pt"])
    script0 = build_parallel_analyse_script(dirs0, parallelism=4, timeout_s=60)
    syntax = subprocess.run(
        ["bash", "-n", "-c", script0],
        capture_output=True, text=True,
    )
    check("generated script passes bash -n", syntax.returncode == 0, syntax.stderr)
    check(
        "script wraps xargs in timeout --kill-after",
        "timeout --kill-after=10 60" in script0
        and "xargs -0 -P 4 -n 1 bash -c" in script0,
    )

    # -- Case 1: all ranks succeed ------------------------------------------
    dirs1 = make_rank_dirs(
        tmp / "case_ok", [f"rank{i}_ascend_pt" for i in range(5)],
    )
    script1 = build_parallel_analyse_script(
        dirs1, parallelism=3, timeout_s=60, py_code=STUB_PY,
    )
    r1 = run_script(script1, env)
    results1 = parse_parallel_results(r1.stdout)
    check("case_ok: driver rc == 0", r1.returncode == 0,
          f"rc={r1.returncode} stderr={r1.stderr[-500:]}")
    check("case_ok: results block present",
          RESULTS_BEGIN in r1.stdout and RESULTS_END in r1.stdout)
    check(
        "case_ok: every rank reported rc 0",
        results1 == {d: 0 for d in dirs1},
        f"got {results1}",
    )
    logs_ok = all(
        (Path(d) / PARALLEL_LOG_NAME).is_file()
        and f"stub analyse of {d}" in (Path(d) / PARALLEL_LOG_NAME).read_text()
        for d in dirs1
    )
    check("case_ok: per-rank analyse_parallel.log captured stdout", logs_ok)

    # -- Case 2: one rank fails ----------------------------------------------
    ok2 = make_rank_dirs(tmp / "case_mixed", ["rank0_ascend_pt", "rank2_ascend_pt"])
    fail_dir = make_rank_dirs(tmp / "case_mixed", ["rank1_fail_ascend_pt"])
    dirs2 = [ok2[0], fail_dir[0], ok2[1]]
    script2 = build_parallel_analyse_script(
        dirs2, parallelism=8, timeout_s=60, py_code=STUB_PY,
    )
    r2 = run_script(script2, env)
    results2 = parse_parallel_results(r2.stdout)
    check("case_fail: driver rc != 0 when any rank fails",
          r2.returncode != 0, f"rc={r2.returncode}")
    check(
        "case_fail: per-rank rc aggregation (7 for the failing rank)",
        results2 is not None
        and results2.get(fail_dir[0]) == 7
        and results2.get(ok2[0]) == 0
        and results2.get(ok2[1]) == 0,
        f"got {results2}",
    )
    fail_log = Path(fail_dir[0]) / PARALLEL_LOG_NAME
    check("case_fail: failing rank still wrote its log",
          fail_log.is_file() and "stub analyse of" in fail_log.read_text())

    # -- Case 3: timeout kills a stuck rank (real timeout(1) only) -----------
    if real_timeout:
        dirs3 = make_rank_dirs(
            tmp / "case_timeout", ["fast_ascend_pt", "slow_ascend_pt"],
        )
        script3 = build_parallel_analyse_script(
            dirs3, parallelism=2, timeout_s=2, py_code=SLEEPY_PY,
        )
        r3 = run_script(script3, env)
        results3 = parse_parallel_results(r3.stdout)
        check("case_timeout: driver rc == 124 (timeout fired)",
              r3.returncode == 124, f"rc={r3.returncode}")
        check(
            "case_timeout: fast rank done, slow rank has no result line",
            results3 is not None
            and results3.get(dirs3[0]) == 0
            and dirs3[1] not in results3,
            f"got {results3}",
        )
        leftover = subprocess.run(
            ["pgrep", "-f", "slow_ascend_pt"],
            capture_output=True, text=True,
        )
        check("case_timeout: no leftover sleep processes",
              leftover.returncode != 0)
    else:
        print("[SKIP] case_timeout: no real GNU timeout(1) on this host")

    # -- Case 4: parser robustness -------------------------------------------
    check("parse: garbage stdout -> None", parse_parallel_results("hello") is None)
    junk = f"{RESULTS_BEGIN}\nnot-a-rc-line\n5\t/some/dir\n{RESULTS_END}\n"
    check("parse: junk lines skipped, valid rows kept",
          parse_parallel_results(junk) == {"/some/dir": 5})

    print()
    if _FAILURES:
        print(f"SELFTEST FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
