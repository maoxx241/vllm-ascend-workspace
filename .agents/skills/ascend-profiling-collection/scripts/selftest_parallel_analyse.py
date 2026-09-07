#!/usr/bin/env python3
"""Local self-test for the parallel analyse driver (no remote, no torch_npu).

``build_parallel_analyse_script`` is a pure function, so the exact bash the
remote container would run can be executed locally against fake
``*_ascend_pt`` directories with a stub python payload. Validates:

1. command shape -- one ``timeout --kill-after``-wrapped ``xargs -P``
   pipeline, generated script parses under ``bash -n``;
2. analyse payload -- each ``--analyse-export`` mode (db/text/both) embeds
   an ``analyse(..., export_type=...)`` call and the on-container Constant
   import in the generated script;
3. output verification -- ``verify_outputs_local`` + ``classify_status``
   db/text/both branches against fake ``ASCEND_PROFILER_OUTPUT`` trees
   (db present/empty/missing, csv/trace present/missing);
4. per-rank logs -- every rank's stdout/stderr lands in
   ``<dir>/analyse_parallel.log``;
5. exit-code aggregation -- ``parse_parallel_results`` recovers every
   per-rank rc; any non-zero rank makes the driver exit non-zero;
6. timeout behaviour (only when a real GNU timeout(1) is available) -- a
   rank stuck past ``timeout_s`` yields rc 124 and no result line for that
   rank while the other ranks still complete;
7. archive branch (--archive-dir) -- ``build_rank_archive_script`` output is
   executed locally against fake rank dirs (the ssh_exec channel is replaced
   with a local bash runner), verifying the archived layout
   ``<archive-dir>/<tag>_<ts>/<rank-basename>/ASCEND_PROFILER_OUTPUT`` plus
   best-effort metadata copies, and ``archive_rank_outputs`` error capture
   (a failing rank flips ``archived`` to False and lands in
   ``archive_error`` without raising).

The real ``ASCEND_ENV_PREAMBLE`` is used unmodified; its
``[ -f /etc/profile.d/vaws-ascend-env.sh ]`` guard is a no-op off-container.

Run: python3 scripts/selftest_parallel_analyse.py
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from collect_torch_profile_case import (  # noqa: E402
    archive_rank_outputs,
    build_rank_archive_script,
    compact_utc_timestamp,
)
import collect_torch_profile_case as collect_case  # noqa: E402
from run_remote_analyse import (  # noqa: E402
    ANALYSE_EXPORT_MODES,
    ANALYSE_PY,
    ASCEND_OUTPUT_DIRNAME,
    CONSTANT_IMPORT,
    PARALLEL_LOG_NAME,
    RESULTS_BEGIN,
    RESULTS_END,
    build_analyse_py,
    build_parallel_analyse_script,
    classify_status,
    parse_parallel_results,
    verify_outputs_local,
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

    # -- Case 0b: per-mode analyse payload embeds export_type ----------------
    mode_expectations = {
        "db": "export_type=Constant.Db",
        "text": "export_type=Constant.Text",
        "both": "export_type=[Constant.Text, Constant.Db]",
    }
    check(
        "mode expectations cover exactly the supported modes",
        set(mode_expectations) == set(ANALYSE_EXPORT_MODES),
    )
    for mode, expr in mode_expectations.items():
        py = build_analyse_py(mode)
        check(f"payload[{mode}]: analyse() call carries {expr}", expr in py)
        check(
            f"payload[{mode}]: on-container Constant import present",
            CONSTANT_IMPORT in py
            and "torch_npu.profiler.analysis.prof_common_func._constant"
            in CONSTANT_IMPORT,
        )
        script_mode = build_parallel_analyse_script(
            dirs0, parallelism=2, timeout_s=60, export_mode=mode,
        )
        check(
            f"script[{mode}]: embeds the shlex-quoted mode payload",
            f"PY_CODE={shlex.quote(py)}" in script_mode,
        )
    check(
        "default ANALYSE_PY is the db payload",
        "export_type=Constant.Db" in ANALYSE_PY,
    )
    check(
        "default script payload matches default ANALYSE_PY",
        f"PY_CODE={shlex.quote(ANALYSE_PY)}" in script0,
    )
    try:
        build_analyse_py("nope")
    except ValueError:
        check("payload: invalid mode raises ValueError", True)
    else:
        check("payload: invalid mode raises ValueError", False)
    try:
        build_parallel_analyse_script(
            dirs0, parallelism=2, timeout_s=60, export_mode="nope",
        )
    except ValueError:
        check("script: invalid export_mode raises ValueError", True)
    else:
        check("script: invalid export_mode raises ValueError", False)

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

    # -- Case 5: verify/classify against local fake rank dirs ----------------
    vroot = tmp / "case_verify"

    def make_rank(name: str) -> Path:
        rank = vroot / name / "rank0_ascend_pt"
        (rank / ASCEND_OUTPUT_DIRNAME).mkdir(parents=True)
        return rank

    # db mode: non-empty db -> ok
    rank = make_rank("db_ok")
    out = rank / ASCEND_OUTPUT_DIRNAME
    db_old = out / "ascend_pytorch_profiler_0_100.db"
    db_new = out / "ascend_pytorch_profiler_0_200.db"
    db_old.write_bytes(b"old")
    db_new.write_bytes(b"new!")
    os.utime(db_old, (1_000_000, 1_000_000))
    os.utime(db_new, (2_000_000, 2_000_000))
    outputs = verify_outputs_local(rank, "db")
    check(
        "verify db: newest non-empty db picked",
        outputs["db_path"]["path"] == str(db_new)
        and outputs["db_path"]["exists"]
        and outputs["db_path"]["non_empty"],
        f"got {outputs['db_path']}",
    )
    check(
        "verify db: csv fields are None in db mode",
        outputs["kernel_details_csv"] is None
        and outputs["trace_view_json"] is None,
    )
    check("verify db: export_type recorded", outputs["export_type"] == "db")
    check("classify db: ok", classify_status(outputs) == "ok")

    # db mode: no db at all -> missing_kernel_details
    rank = make_rank("db_missing")
    outputs = verify_outputs_local(rank, "db")
    check(
        "verify db: no db file -> path None, exists False",
        outputs["db_path"]["path"] is None
        and outputs["db_path"]["exists"] is False
        and outputs["db_path"]["non_empty"] is False,
    )
    check(
        "classify db: missing -> missing_kernel_details",
        classify_status(outputs) == "missing_kernel_details",
    )

    # db mode: empty db -> missing_kernel_details (exists but not non_empty)
    rank = make_rank("db_empty")
    (rank / ASCEND_OUTPUT_DIRNAME / "ascend_pytorch_profiler_0_1.db").write_bytes(b"")
    outputs = verify_outputs_local(rank, "db")
    check(
        "verify db: empty db -> exists True, non_empty False",
        outputs["db_path"]["exists"] is True
        and outputs["db_path"]["non_empty"] is False,
    )
    check(
        "classify db: empty -> missing_kernel_details",
        classify_status(outputs) == "missing_kernel_details",
    )

    # db mode: ASCEND_PROFILER_OUTPUT dir itself absent -> missing_kernel_details
    rank_nodir = vroot / "db_no_output_dir" / "rank0_ascend_pt"
    rank_nodir.mkdir(parents=True)
    outputs = verify_outputs_local(rank_nodir, "db")
    check(
        "classify db: no output dir -> missing_kernel_details",
        outputs["db_path"]["exists"] is False
        and classify_status(outputs) == "missing_kernel_details",
    )

    # text mode: historical csv + trace_view checks
    rank = make_rank("text_ok")
    out = rank / ASCEND_OUTPUT_DIRNAME
    (out / "kernel_details.csv").write_text("header\n", encoding="utf-8")
    (out / "trace_view.json").write_text("{}\n", encoding="utf-8")
    outputs = verify_outputs_local(rank, "text")
    check(
        "verify text: csv + trace_view exist, db_path None",
        outputs["kernel_details_csv"]["exists"]
        and outputs["trace_view_json"]["exists"]
        and outputs["db_path"] is None
        and outputs["export_type"] == "text",
    )
    check("classify text: ok", classify_status(outputs) == "ok")

    rank = make_rank("text_no_trace")
    (rank / ASCEND_OUTPUT_DIRNAME / "kernel_details.csv").write_text("h\n", encoding="utf-8")
    outputs = verify_outputs_local(rank, "both")
    check(
        "classify both: trace_view missing -> partial",
        classify_status(outputs) == "partial",
    )

    rank = make_rank("text_no_csv")
    (rank / ASCEND_OUTPUT_DIRNAME / "trace_view.json").write_text("{}\n", encoding="utf-8")
    outputs = verify_outputs_local(rank, "text")
    check(
        "classify text: csv missing -> missing_kernel_details",
        classify_status(outputs) == "missing_kernel_details",
    )

    # legacy outputs shape (no export_type key) still classifies as before
    legacy = {
        "kernel_details_csv": {"path": "x", "exists": True},
        "trace_view_json": {"path": "y", "exists": False},
    }
    check("classify legacy shape -> partial", classify_status(legacy) == "partial")
    legacy["trace_view_json"]["exists"] = True
    check("classify legacy shape -> ok", classify_status(legacy) == "ok")

    # -- Case 6: archive branch (--archive-dir), local fake-container run ----
    # The ssh_exec channel is replaced with a local bash runner so the exact
    # script the container would execute is exercised against fake rank dirs.
    check(
        "archive: compact_utc_timestamp strips - and :",
        compact_utc_timestamp("2026-09-07T03:36:45Z") == "20260907T033645Z",
    )

    aroot = tmp / "case_archive"
    arch_base = aroot / "shared" / "archives"

    def make_archive_rank(name: str, *, metadata: bool = True) -> str:
        rank = aroot / "src" / name
        out = rank / ASCEND_OUTPUT_DIRNAME
        out.mkdir(parents=True)
        (out / "ascend_pytorch_profiler_0_1.db").write_bytes(b"db-bytes")
        if metadata:
            (rank / "profiler_info_rank0.json").write_text("{}\n", encoding="utf-8")
            (rank / "profiler_metadata.json").write_text("{}\n", encoding="utf-8")
        return str(rank)

    rank_a = make_archive_rank("r0_20260907_033645_rank0_ascend_pt")
    rank_b = make_archive_rank("r0_20260907_033646_rank1_ascend_pt")
    rank_nometa = make_archive_rank(
        "r0_20260907_033647_rank2_ascend_pt", metadata=False,
    )

    script_a = build_rank_archive_script(rank_a, f"{arch_base}/dest_a")
    check(
        "archive: script mkdirs dest and cp -r ASCEND_PROFILER_OUTPUT",
        "mkdir -p" in script_a
        and f"cp -r {shlex.quote(rank_a + '/ASCEND_PROFILER_OUTPUT')}" in script_a
        and "profiler_info_*.json" in script_a
        and "profiler_metadata.json" in script_a,
    )
    syntax = subprocess.run(
        ["bash", "-n", "-c", script_a], capture_output=True, text=True,
    )
    check("archive: generated script passes bash -n", syntax.returncode == 0,
          syntax.stderr)

    def local_ssh_exec(ep, script, *, check=True, timeout=None):  # noqa: A002
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=timeout,
        )

    orig_ssh_exec = collect_case.ssh_exec
    collect_case.ssh_exec = local_ssh_exec
    try:
        started_at = "2026-09-07T03:36:45Z"
        res = archive_rank_outputs(
            None, [rank_a, rank_b, rank_nometa], str(arch_base),
            tag="demo-tag", started_at=started_at,
        )
    finally:
        collect_case.ssh_exec = orig_ssh_exec

    expected_root = f"{arch_base}/demo-tag_20260907T033645Z"
    check("archive: archive_dir is <archive-dir>/<tag>_<compact ts>",
          res["archive_dir"] == expected_root, f"got {res['archive_dir']}")
    check("archive: all ranks archived -> archived True, no error",
          res["archived"] is True and res["archive_error"] is None,
          f"got {res}")
    per_rank = {r["path"]: r["archived_path"] for r in res["ranks"]}
    layout_ok = True
    for rank in (rank_a, rank_b, rank_nometa):
        basename = Path(rank).name
        dest = Path(expected_root) / basename
        layout_ok = layout_ok and (
            per_rank[rank] == str(dest)
            and (dest / ASCEND_OUTPUT_DIRNAME / "ascend_pytorch_profiler_0_1.db").read_bytes() == b"db-bytes"
        )
    check("archive: per-rank archived_path + ASCEND_PROFILER_OUTPUT layout",
          layout_ok, f"got {per_rank}")
    check(
        "archive: metadata files copied when present",
        (Path(expected_root) / Path(rank_a).name / "profiler_info_rank0.json").is_file()
        and (Path(expected_root) / Path(rank_a).name / "profiler_metadata.json").is_file(),
    )
    check(
        "archive: missing metadata does not fail the rank",
        per_rank[rank_nometa] is not None,
    )

    # One rank dir that does not exist -> cp fails, error captured, no raise.
    missing_rank = str(aroot / "src" / "ghost_ascend_pt")
    collect_case.ssh_exec = local_ssh_exec
    try:
        res_fail = archive_rank_outputs(
            None, [rank_a, missing_rank], str(arch_base),
            tag="demo-fail", started_at=started_at,
        )
    finally:
        collect_case.ssh_exec = orig_ssh_exec
    fail_per_rank = {r["path"]: r["archived_path"] for r in res_fail["ranks"]}
    check(
        "archive: failing rank -> archived False + archive_error, other rank kept",
        res_fail["archived"] is False
        and res_fail["archive_error"] is not None
        and missing_rank in (res_fail["archive_error"] or "")
        and fail_per_rank[missing_rank] is None
        and fail_per_rank[rank_a] is not None,
        f"got {res_fail}",
    )

    print()
    if _FAILURES:
        print(f"SELFTEST FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
