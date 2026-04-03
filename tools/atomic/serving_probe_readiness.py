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
from tools.lib.serving_lifecycle import probe_service_readiness


def probe_service(paths: RepoPaths, service_id: str, *, timeout_s: float = 180.0) -> dict[str, object]:
    try:
        service = probe_service_readiness(paths, service_id, timeout_s=timeout_s)
    except RuntimeError as exc:
        return validate_tool_result(
            {
                "status": "needs_repair",
                "observations": [f"service {service_id} is not ready"],
                "reason": str(exc),
                "next_probes": ["serving.describe_session", "serving.stop_service"],
                "idempotent": True,
                "payload": {"service_id": service_id},
            },
            action_kind="probe",
        )

    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"service {service_id} is ready"],
            "idempotent": True,
            "payload": {
                "service_id": service["service_id"],
                "health_status": service["health_status"],
                "server_root_url": service["server_root_url"],
            },
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="serving.probe_readiness")
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--timeout-s", type=float, default=180.0)
    args = parser.parse_args(argv)
    result = probe_service(RepoPaths(root=Path.cwd()), args.service_id, timeout_s=args.timeout_s)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
