"""Task-facing operations exposed identically to every remote-dev MCP client."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .result import make_result

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_task_client import TaskClient


def vaws_call(name, args):
    started = time.monotonic()
    target = {"kind": "vaws-task"}
    try:
        client = TaskClient(args.get("context_file", ""))
        target["session_id"] = client.context["session"]["id"]
        if name == "vaws.session":
            if args.get("sources"):
                client.sources(args["sources"])
            value = client.status()
            status = value["session"]["state"]
        elif name == "vaws.run":
            value = client.run(**{key: args[key] for key in ("request_id", "command", "profile_key", "runtime_id", "devices", "npu_count", "env", "timeout_seconds") if key in args})
            status = value["state"]
        elif name == "vaws.execution":
            value = client.observe(args["execution_id"], args.get("action", "status"), args.get("force", False))
            status = value["state"]
        elif name == "vaws.finish":
            value = client.finish(args.get("force", False))
            status = value["state"]
        else:
            raise ValueError("unknown VAWS operation")
        outcome = "blocked" if status in {"uncertain", "waiting_for_runtime"} else "failed" if status == "failed" else "timeout" if status == "timeout" else "success"
        result = make_result(tool=name, target=target, outcome=outcome, status=status,
                             summary="VAWS " + status.replace("_", " "),
                             duration_ms=int((time.monotonic() - started) * 1000), extra={"data": value})
    except Exception as exc:
        result = make_result(tool=name, target=target, outcome="blocked", status="unavailable",
                             summary=str(exc), duration_ms=int((time.monotonic() - started) * 1000),
                             warnings=["Local file and shell tools remain available. No remote success is implied."])
    return {"text": json.dumps(result, ensure_ascii=False), "result": result}
