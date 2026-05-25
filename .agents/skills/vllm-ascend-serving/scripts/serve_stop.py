#!/usr/bin/env python3
"""Stop a vllm-ascend service on a workspace-managed remote container.

Usage:
    python3 serve_stop.py --machine <alias>
    python3 serve_stop.py --session-id <id>
    python3 serve_stop.py --machine <alias> --force

Progress on stderr, final JSON on stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    emit_progress,
    load_serving_state,
    now_utc,
    print_json,
    resolve_execution_target,
    save_serving_state,
    ssh_exec,
)
from vaws_session_state import file_lock, release_service_port, session_lock_dir


GRACE_PERIOD_SECONDS = 5


def check_alive(ep, pid: int) -> bool:
    r = ssh_exec(ep, f"kill -0 {pid} 2>/dev/null && echo alive || echo dead", check=False)
    return r.stdout.strip() == "alive"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--machine", help="machine alias or host IP")
    p.add_argument("--session-id", help="VAWS session id")
    p.add_argument("--session-file", help="explicit session.json path")
    p.add_argument("--force", action="store_true", help="use SIGKILL if graceful stop fails")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock_stack = contextlib.ExitStack()

    try:
        target = resolve_execution_target(
            args.machine,
            session_id=args.session_id,
            session_file=args.session_file,
        )
        alias = target.alias
        ep = target.endpoint
        if target.session_id:
            emit_progress("lock", f"acquiring serving lock for session {target.session_id}")
            lock_stack.enter_context(
                file_lock(session_lock_dir(target.state_repo_root) / f"{target.session_id}.serving.lock")
            )

        state = load_serving_state(
            alias,
            session_id=target.session_id,
            state_repo_root=target.state_repo_root,
        )
        if state is None:
            print_json({
                "status": "not_found",
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
                "message": "no serving state recorded for this machine",
            })
            return 0

        pid = state.get("pid")
        if not pid:
            print_json({
                "status": "not_found",
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
                "message": "serving state has no pid",
            })
            return 0

        alive = check_alive(ep, pid)
        if not alive:
            emit_progress("stop", f"pid={pid} is already gone")
            state["status"] = "stopped"
            state["stopped_at"] = now_utc()
            save_serving_state(
                alias,
                state,
                session_id=target.session_id,
                state_repo_root=target.state_repo_root,
            )
            if target.session_id:
                release_service_port(
                    repo_root=target.state_repo_root,
                    machine_alias=alias,
                    session_id=target.session_id,
                    port=state.get("port"),
                )
            print_json({
                "status": "stopped",
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
                "pid": pid,
                "message": "process was already stopped",
            })
            return 0

        # SIGINT first (graceful)
        emit_progress("stop", f"sending SIGINT to pid={pid}")
        ssh_exec(ep, f"kill -2 {pid} 2>/dev/null || true", check=False)
        time.sleep(GRACE_PERIOD_SECONDS)

        if check_alive(ep, pid):
            emit_progress("stop", f"still alive, sending SIGTERM to pid={pid}")
            ssh_exec(ep, f"kill -15 {pid} 2>/dev/null || true", check=False)
            time.sleep(GRACE_PERIOD_SECONDS)

        if check_alive(ep, pid):
            if args.force:
                emit_progress("stop", f"still alive, sending SIGKILL to pid={pid}")
                ssh_exec(ep, f"kill -9 {pid} 2>/dev/null || true", check=False)
                time.sleep(1)
            else:
                print_json({
                    "status": "failed",
                    "machine": alias,
                    "mode": target.mode,
                    "session_id": target.session_id,
                    "pid": pid,
                    "error": f"process {pid} did not exit after SIGINT+SIGTERM; rerun with --force to SIGKILL",
                })
                return 1

        stopped = not check_alive(ep, pid)
        state["status"] = "stopped" if stopped else "alive"
        state["stopped_at"] = now_utc()
        save_serving_state(
            alias,
            state,
            session_id=target.session_id,
            state_repo_root=target.state_repo_root,
        )
        if stopped and target.session_id:
            release_service_port(
                repo_root=target.state_repo_root,
                machine_alias=alias,
                session_id=target.session_id,
                port=state.get("port"),
            )

        output: dict[str, Any] = {
            "status": "stopped" if stopped else "failed",
            "machine": alias,
            "mode": target.mode,
            "session_id": target.session_id,
            "pid": pid,
            "stopped": stopped,
        }
        if not stopped:
            output["error"] = "process refused to exit"

        print_json(output)
        return 0 if stopped else 1

    except Exception as exc:
        print_json({
            "status": "failed",
            "error": str(exc),
            "machine": getattr(args, "machine", None),
            "session_id": getattr(args, "session_id", None),
        })
        return 2
    finally:
        lock_stack.close()


if __name__ == "__main__":
    raise SystemExit(main())
