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
from tools.lib.serving_lifecycle import launch_service_session


def launch_service(
    paths: RepoPaths,
    *,
    server_name: str,
    preset_name: str,
    weights_path: str,
    api_key_env: str | None,
    lifecycle: str = "explicit-serving",
) -> dict[str, object]:
    try:
        service = launch_service_session(
            paths,
            server_name=server_name,
            preset_name=preset_name,
            weights_path=weights_path,
            api_key_env=api_key_env,
            lifecycle=lifecycle,
        )
    except RuntimeError as exc:
        return validate_tool_result(
            {
                "status": "execute_failed",
                "observations": [f"service launch failed for {server_name}"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "payload": {
                    "server_name": server_name,
                    "preset_name": preset_name,
                },
            },
            action_kind="execute",
        )

    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"service {service['service_id']} launched for {server_name}"],
            "side_effects": ["service process launched", "service session recorded"],
            "payload": {
                "service_id": service["service_id"],
                "server_name": service["server_name"],
                "server_root_url": service["server_root_url"],
                "lifecycle": service["lifecycle"],
                "manifest_path": service["manifest_path"],
            },
        },
        action_kind="execute",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="serving.launch_service")
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--preset", required=True)
    parser.add_argument("--weights-path", required=True)
    parser.add_argument("--api-key-env")
    parser.add_argument("--lifecycle", default="explicit-serving")
    args = parser.parse_args(argv)
    result = launch_service(
        RepoPaths(root=Path.cwd()),
        server_name=args.server_name,
        preset_name=args.preset,
        weights_path=args.weights_path,
        api_key_env=args.api_key_env,
        lifecycle=args.lifecycle,
    )
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
