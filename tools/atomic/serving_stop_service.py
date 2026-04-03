from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.config import RepoPaths
from tools.lib.serving_lifecycle import stop_service_session


def stop_service(paths: RepoPaths, service_id: str) -> dict[str, object]:
    try:
        stop_service_session(paths, service_id)
    except RuntimeError as exc:
        return validate_tool_result(
            {
                "status": "cleanup_failed",
                "observations": [f"failed to stop service {service_id}"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": True,
                "payload": {"service_id": service_id},
            },
            action_kind="cleanup",
        )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"service {service_id} stopped"],
            "side_effects": ["service process terminated", "service session removed"],
            "idempotent": True,
            "payload": {"service_id": service_id},
        },
        action_kind="cleanup",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="serving.stop_service")
    parser.add_argument("--service-id", required=True)
    args = parser.parse_args(argv)
    result = stop_service(RepoPaths(root=Path.cwd()), args.service_id)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
