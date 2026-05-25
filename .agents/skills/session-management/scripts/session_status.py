#!/usr/bin/env python3
"""Inspect one VAWS session."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_session_state import load_session_lookup, session_serving_state_path  # noqa: E402

SSH_CHECK_TIMEOUT_SECONDS = 60


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def tail_output(value: str | bytes | None, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-limit:]


def ssh_check(host: str, port: int, user: str = "root", script: str = "true") -> dict[str, Any]:
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(port),
        f"{user}@{host}",
        "bash",
        "-c",
        shlex.quote(script),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "timed_out": True,
            "timeout_seconds": SSH_CHECK_TIMEOUT_SECONDS,
            "stdout_tail": tail_output(exc.stdout),
            "stderr_tail": tail_output(exc.stderr),
        }
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "timed_out": False,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
    }


def load_serving(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--session-id")
    parser.add_argument("--session-file")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        lookup = load_session_lookup(
            session_id=args.session_id,
            session_file=args.session_file,
            repo_root=ROOT,
        )
        session = lookup.session
        sid = session["session_id"]
        local_root = Path(session["local"]["worktree_root"])
        remote = session["remote"]
        container = remote["container"]
        serving = load_serving(session_serving_state_path(sid, lookup.state_repo_root))
        container_ssh = ssh_check(
            remote["host"],
            int(container["ssh_port"]),
            "root",
            "true",
        )
        service: dict[str, Any] | None = None
        if serving and serving.get("pid"):
            pid = int(serving["pid"])
            service = ssh_check(
                remote["host"],
                int(container["ssh_port"]),
                "root",
                f"kill -0 {pid} 2>/dev/null",
            )
        status = session.get("status", "unknown")
        if status == "ready" and not local_root.exists():
            status = "needs_repair"
        if status == "ready" and not container_ssh["ok"]:
            status = "needs_repair"
        print_json(
            {
                "status": status,
                "session_id": sid,
                "session_file": str(lookup.session_file),
                "worktree": {"path": str(local_root), "exists": local_root.exists()},
                "container": {
                    "name": container["name"],
                    "ssh_port": container["ssh_port"],
                    "ssh": container_ssh,
                },
                "serving": serving,
                "service_alive": service,
                "session": session,
            }
        )
        return 0 if status in {"ready", "planned", "stopped", "removed"} else 1
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
