#!/usr/bin/env python3
"""Run lightweight VAWS remote-toolbox stress checks.

This intentionally avoids NPU-heavy workloads, runtime install/rebuild, and
benchmarks. It focuses on agent-facing mechanics: concurrent exec, job
observation, artifact manifest/pull, and cleanup proof.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_remote_toolbox import (  # noqa: E402
    ARTIFACT_STATE_DIR,
    RemoteToolboxError,
    artifact_pull,
    duration_ms,
    emit_progress,
    print_json,
    remote_exec,
    remote_job_status,
    remote_job_tail,
    resolve_remote_target,
    start_remote_job,
    tail_text,
    utc_now_iso,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--machine", help="machine alias or host IP")
    parser.add_argument("--session-id", help="VAWS session id")
    parser.add_argument("--session-file", help="explicit session.json path")
    parser.add_argument("--parallelism", type=int, default=4)
    parser.add_argument("--exec-count", type=int, default=8)
    parser.add_argument("--job-count", type=int, default=4)
    parser.add_argument("--artifact-files", type=int, default=32)
    parser.add_argument("--artifact-bytes", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--skip-exec", action="store_true")
    parser.add_argument("--skip-jobs", action="store_true")
    parser.add_argument("--skip-artifacts", action="store_true")
    parser.add_argument("--cleanup-remote", action=argparse.BooleanOptionalAction, default=True)
    return parser


def _case(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, **extra}


def _run_parallel(items: list[Any], worker, *, parallelism: int) -> list[Any]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
        futures = [pool.submit(worker, item) for item in items]
        return [future.result() for future in concurrent.futures.as_completed(futures)]


def run_stress(args: argparse.Namespace) -> dict[str, Any]:
    started_at = utc_now_iso()
    start = time.monotonic()
    target = resolve_remote_target(
        machine=args.machine,
        session_id=args.session_id,
        session_file=args.session_file,
    )
    cases: dict[str, Any] = {}

    if not args.skip_exec:
        emit_progress("stress-exec", "running concurrent remote_exec checks", count=args.exec_count)

        def exec_worker(i: int) -> dict[str, Any]:
            result = remote_exec(
                target,
                command=f"printf exec-{i}",
                timeout=args.timeout,
                log_kind="stress-exec",
            )
            return {
                "index": i,
                "status": result["status"],
                "exit_code": result["exit_code"],
                "duration_ms": result["duration_ms"],
                "stdout_tail": result["stdout_tail"],
                "logs": result["logs"],
            }

        exec_results = _run_parallel(list(range(args.exec_count)), exec_worker, parallelism=args.parallelism)
        cases["remote_exec"] = _case(
            "ok" if all(item["status"] == "ok" and item["stdout_tail"].startswith("exec-") for item in exec_results) else "failed",
            results=sorted(exec_results, key=lambda item: item["index"]),
        )

    if not args.skip_jobs:
        emit_progress("stress-jobs", "starting and observing remote jobs", count=args.job_count)
        job_starts = []
        for i in range(args.job_count):
            job_starts.append(
                start_remote_job(
                    target,
                    kind="stress",
                    command=f"printf job-{i}",
                    timeout_seconds=int(args.timeout),
                    job_id=None,
                )
            )
        job_observed = []
        for item in job_starts:
            record = {
                "job_id": item["job_id"],
                "remote_dir": item["remote_dir"],
                "target": target.to_dict(),
                "started_at": item["started_at"],
                "kind": "stress",
            }
            deadline = time.monotonic() + args.timeout
            status_payload = remote_job_status(target, record)
            while status_payload["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.5)
                status_payload = remote_job_status(target, record)
            tail_payload = remote_job_tail(target, record, lines=5)
            job_observed.append(
                {
                    "job_id": item["job_id"],
                    "start_status": item["status"],
                    "status": status_payload["status"],
                    "tail": tail_payload.get("tail", ""),
                    "logs": item["logs"],
                }
            )
        cases["remote_jobs"] = _case(
            "ok" if all(item["status"] == "succeeded" and "job-" in item["tail"] for item in job_observed) else "failed",
            results=job_observed,
        )

    if not args.skip_artifacts:
        emit_progress("stress-artifacts", "creating and pulling artifact directory", files=args.artifact_files)
        remote_dir = f"{target.remote_toolbox_root()}/stress/artifacts-{int(time.time())}"
        local_dir = ARTIFACT_STATE_DIR / "stress" / target.target_id / Path(remote_dir).name
        make_script = [
            f"rm -rf {shlex.quote(remote_dir)}",
            f"mkdir -p {shlex.quote(remote_dir)}",
        ]
        for i in range(args.artifact_files):
            make_script.append(
                f"python3 - <<'PY' > {shlex.quote(remote_dir + f'/file-{i:04d}.txt')}\n"
                f"import sys\nsys.stdout.write('file-{i}-' + 'x' * {max(0, args.artifact_bytes - len(str(i)) - 6)})\n"
                "PY"
            )
        create_result = remote_exec(
            target,
            command="\n".join(make_script),
            timeout=max(args.timeout, 120),
            log_kind="stress-artifact-create",
        )
        pull_result = artifact_pull(
            target,
            remote_path=remote_dir,
            local_dir=local_dir,
            timeout=max(args.timeout, 120),
        )
        cleanup_result = None
        if args.cleanup_remote:
            cleanup_result = remote_exec(
                target,
                command=f"rm -rf {shlex.quote(remote_dir)}",
                timeout=args.timeout,
                log_kind="stress-artifact-cleanup",
            )
        cases["artifacts"] = _case(
            "ok" if create_result["status"] == "ok" and pull_result["status"] == "ok" and pull_result["artifacts"]["manifest"]["file_count"] == args.artifact_files else "failed",
            remote_dir=remote_dir,
            local_dir=str(local_dir),
            create_status=create_result["status"],
            pull_status=pull_result["status"],
            file_count=pull_result.get("artifacts", {}).get("manifest", {}).get("file_count"),
            pulled=len(pull_result.get("artifacts", {}).get("pulled", [])),
            transports=sorted({item.get("transport") for item in pull_result.get("artifacts", {}).get("pulled", [])}),
            cleanup_status=cleanup_result["status"] if cleanup_result else "skipped",
        )

    failed = {name: case for name, case in cases.items() if case.get("status") != "ok"}
    return {
        "status": "ok" if not failed else "failed",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "config": {
            "parallelism": args.parallelism,
            "exec_count": args.exec_count,
            "job_count": args.job_count,
            "artifact_files": args.artifact_files,
            "artifact_bytes": args.artifact_bytes,
        },
        "cases": cases,
        "failed_cases": failed,
        "logs": {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    started_at = utc_now_iso()
    start = time.monotonic()
    try:
        payload = run_stress(args)
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        print_json({
            "status": "failed",
            "started_at": started_at,
            "duration_ms": duration_ms(start),
            "error": str(exc),
            "error_tail": tail_text(str(exc), 2000),
            "target": None,
            "logs": {},
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
