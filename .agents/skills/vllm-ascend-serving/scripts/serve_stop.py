#!/usr/bin/env python3
"""Stop a vllm-ascend service on a workspace-managed remote container.

Usage:
    python3 serve_stop.py --machine <alias>
    python3 serve_stop.py --machine <alias> --force

Progress on stderr, final JSON on stdout.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    container_endpoint,
    emit_progress,
    load_serving_state,
    now_utc,
    print_json,
    resolve_machine,
    save_serving_state,
    ssh_exec,
)


GRACE_PERIOD_SECONDS = 5


def check_alive(ep, pid: int) -> bool:
    r = ssh_exec(ep, f"kill -0 {pid} 2>/dev/null && echo alive || echo dead", check=False)
    return r.stdout.strip() == "alive"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--machine", required=True, help="machine alias or host IP")
    p.add_argument("--force", action="store_true", help="use SIGKILL if graceful stop fails")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        record = resolve_machine(args.machine)
        alias = record["alias"]
        ep = container_endpoint(record)

        state = load_serving_state(alias)
        if state is None:
            print_json({
                "status": "not_found",
                "machine": alias,
                "message": "no serving state recorded for this machine",
            })
            return 0

        pid = state.get("pid")
        if not pid:
            print_json({
                "status": "not_found",
                "machine": alias,
                "message": "serving state has no pid",
            })
            return 0

        alive = check_alive(ep, pid)
        if not alive:
            emit_progress("stop", f"pid={pid} is already gone")
            state["status"] = "stopped"
            state["stopped_at"] = now_utc()
            save_serving_state(alias, state)
            print_json({
                "status": "stopped",
                "machine": alias,
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
                    "pid": pid,
                    "error": f"process {pid} did not exit after SIGINT+SIGTERM; rerun with --force to SIGKILL",
                })
                return 1

        stopped = not check_alive(ep, pid)
        state["status"] = "stopped" if stopped else "alive"
        state["stopped_at"] = now_utc()
        save_serving_state(alias, state)

        output: dict[str, Any] = {
            "status": "stopped" if stopped else "failed",
            "machine": alias,
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
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
