#!/usr/bin/env python3
"""Stop a coordinator-owned vllm-ascend service. The user container remains."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import SERVICE_NAME, emit_progress, print_json  # noqa: E402
from vaws_task_target import DONE, executions_for_service, task_client, task_id_of  # noqa: E402


def pick_id(client, service: str, execution_id: str | None) -> str | None:
    if execution_id:
        return execution_id
    if not service:
        return None
    rows = executions_for_service(client, service)
    if not rows:
        return None
    return str(rows[-1].get("id") or rows[-1].get("execution_id") or "") or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--context-file")
    parser.add_argument("--execution-id")
    parser.add_argument("--service", default=SERVICE_NAME)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = task_client(args.context_file)
        task_id = task_id_of(client)
        execution_id = pick_id(client, args.service, args.execution_id)
        if not execution_id:
            print_json({
                "status": "not_found",
                "task_id": task_id,
                "service": args.service,
                "container_preserved": True,
            })
            return 0
        emit_progress("stop", f"stopping execution {execution_id}")
        result = client.observe(execution_id, "stop", args.force)
        state = str(result.get("state") or "")
        terminal = state in DONE
        print_json({
            "status": "stopped" if terminal else "incomplete",
            "task_id": task_id,
            "service": args.service,
            "execution_id": execution_id,
            "state": state,
            "container_preserved": True,
            "result": result,
            **({} if terminal else {"error": result.get("error") or "stop did not confirm a terminal state"}),
        })
        return 0 if terminal else 1
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
