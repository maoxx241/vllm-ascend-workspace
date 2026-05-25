#!/usr/bin/env python3
"""Check the status of a running vllm-ascend service.

Usage:
    python3 serve_status.py --machine <alias>
    python3 serve_status.py --session-id <id>

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
    emit_progress,
    load_serving_state,
    now_utc,
    print_json,
    resolve_execution_target,
    save_serving_state,
    ssh_exec,
)
from vaws_session_state import release_service_port


def check_alive(ep, pid: int) -> bool:
    r = ssh_exec(ep, f"kill -0 {pid} 2>/dev/null && echo alive || echo dead", check=False)
    return r.stdout.strip() == "alive"


def check_health(ep, port: int) -> bool:
    script = (
        f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3"
        f" http://127.0.0.1:{port}/health 2>/dev/null || echo 000"
    )
    r = ssh_exec(ep, script, check=False)
    return r.stdout.strip() == "200"


def check_models(ep, port: int) -> dict[str, Any] | None:
    script = f"curl -s --connect-timeout 3 http://127.0.0.1:{port}/v1/models 2>/dev/null || true"
    r = ssh_exec(ep, script, check=False)
    text = r.stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if data.get("data") else None
    except json.JSONDecodeError:
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--machine", help="machine alias or host IP")
    p.add_argument("--session-id", help="VAWS session id")
    p.add_argument("--session-file", help="explicit session.json path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        target = resolve_execution_target(
            args.machine,
            session_id=args.session_id,
            session_file=args.session_file,
        )
        alias = target.alias
        ep = target.endpoint

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
        port = state.get("port")
        if not pid or not port:
            print_json({
                "status": "not_found",
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
                "message": "serving state is missing pid or port",
                "state": state,
            })
            return 0

        emit_progress("probe", f"checking pid={pid} port={port}")

        alive = check_alive(ep, pid)
        health = check_health(ep, port) if alive else False
        models = check_models(ep, port) if health else None

        if alive and health and models is not None:
            status = "ready"
        elif alive and health:
            status = "alive_healthy"
        elif alive:
            status = "alive"
        else:
            status = "stopped"

        state["status"] = status
        state["status_checked_at"] = now_utc()
        if status == "stopped":
            state["stopped_at"] = state.get("stopped_at") or state["status_checked_at"]
        save_serving_state(
            alias,
            state,
            session_id=target.session_id,
            state_repo_root=target.state_repo_root,
        )
        if status == "stopped" and target.session_id:
            release_service_port(
                repo_root=target.state_repo_root,
                machine_alias=alias,
                session_id=target.session_id,
                port=state.get("port"),
            )

        output: dict[str, Any] = {
            "status": status,
            "machine": alias,
            "mode": target.mode,
            "session_id": target.session_id,
            "alive": alive,
            "health": health,
            "models_ok": models is not None,
            "pid": pid,
            "port": port,
            "base_url": state.get("base_url"),
            "served_model_name": state.get("served_model_name"),
            "model": state.get("model"),
            "log_stdout": state.get("log_stdout"),
            "log_stderr": state.get("log_stderr"),
            "runtime_dir": state.get("runtime_dir"),
            "started_at": state.get("started_at"),
        }

        if not alive:
            stderr_path = state.get("log_stderr")
            if stderr_path:
                r = ssh_exec(
                    ep,
                    f"tail -20 {shlex.quote(stderr_path)} 2>/dev/null || echo '(no log)'",
                    check=False,
                )
                output["stderr_tail"] = r.stdout.strip()

        print_json(output)
        return 0

    except Exception as exc:
        print_json({
            "status": "failed",
            "error": str(exc),
            "machine": getattr(args, "machine", None),
            "session_id": getattr(args, "session_id", None),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
