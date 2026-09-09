#!/usr/bin/env python3
"""Read coordinator facts for a vllm-ascend service, then optional business health."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import SERVICE_NAME, emit_progress, endpoint_from_reply, print_json, service_port_of, ssh_exec  # noqa: E402
from vaws_task_target import DONE, PENDING, RUNNING, executions_for_service, task_client, task_id_of  # noqa: E402


def check_health(ep, port: int) -> bool:
    script = (
        f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3 --max-time 5"
        f" http://127.0.0.1:{port}/health 2>/dev/null || echo 000"
    )
    return ssh_exec(ep, script, check=False).stdout.strip() == "200"


def check_models(ep, port: int) -> dict[str, Any] | None:
    script = f"curl -s --connect-timeout 3 --max-time 5 http://127.0.0.1:{port}/v1/models 2>/dev/null || true"
    text = ssh_exec(ep, script, check=False).stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if data.get("data") else None
    except json.JSONDecodeError:
        return None


def classify(state: str) -> str:
    if state in DONE:
        return "terminal"
    if state in RUNNING:
        return "running"
    if state in PENDING:
        return "pending"
    return "pending"


def pick_execution(client, service: str, execution_id: str | None) -> dict[str, Any] | None:
    if execution_id:
        return client.observe(execution_id, "status")
    if not service:
        return None
    rows = executions_for_service(client, service)
    if not rows:
        return None
    row = rows[-1]
    eid = row.get("id") or row.get("execution_id")
    return client.observe(eid, "status") if eid else row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--context-file")
    parser.add_argument("--execution-id")
    parser.add_argument("--service", default=SERVICE_NAME)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = task_client(args.context_file)
        task_id = task_id_of(client)
        observation = pick_execution(client, args.service, args.execution_id)
        if not observation:
            print_json({
                "status": "not_found",
                "task_id": task_id,
                "service": args.service,
                "message": "coordinator has no matching service execution",
            })
            return 0
        state = str(observation.get("state") or observation.get("phase") or "")
        kind = classify(state)
        execution_id = observation.get("execution_id") or observation.get("id")
        output: dict[str, Any] = {
            "task_id": task_id,
            "service": args.service,
            "execution_id": execution_id,
            "state": state,
            "observation": observation,
            "running": kind == "running",
            "ready": False,
        }
        if kind == "pending":
            output["status"] = "queued"
            print_json(output)
            return 0
        if kind == "terminal":
            output["status"] = "stopped" if state == "cancelled" else state
            print_json(output)
            return 0
        emit_progress("probe", f"execution {execution_id} is running")
        port = service_port_of(observation)
        if port is None:
            output["status"] = "incomplete"
            output["error"] = "running execution has no service port"
            print_json(output)
            return 0
        try:
            endpoint = endpoint_from_reply(observation)
        except Exception as exc:
            output["status"] = "incomplete"
            output["error"] = str(exc)
            print_json(output)
            return 0
        health = check_health(endpoint, port)
        models = check_models(endpoint, port) if health else None
        if health and models is not None:
            output["status"] = "ready"
            output["ready"] = True
        elif health:
            output["status"] = "alive_healthy"
        else:
            output["status"] = "running"
        output["port"] = port
        output["health"] = health
        output["models_ok"] = models is not None
        output["base_url"] = f"http://{endpoint.host}:{port}"
        print_json(output)
        return 0
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
