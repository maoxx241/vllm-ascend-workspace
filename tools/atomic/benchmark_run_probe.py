from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.benchmark_execution import run_benchmark_probe
from tools.lib.config import RepoPaths


def run_probe(
    paths: RepoPaths,
    *,
    server_name: str,
    preset_name: str,
    service_id: str | None,
) -> dict[str, object]:
    if not service_id:
        return validate_tool_result(
            {
                "status": "needs_input",
                "observations": ["benchmark.run_probe requires an existing service session"],
                "reason": "service_id is required for benchmark.run_probe",
                "side_effects": [],
                "retryable": False,
                "payload": {
                    "server_name": server_name,
                    "preset_name": preset_name,
                },
            },
            action_kind="execute",
        )

    try:
        payload = run_benchmark_probe(
            paths,
            server_name=server_name,
            preset_name=preset_name,
            service_id=service_id,
        )
    except RuntimeError as exc:
        return validate_tool_result(
            {
                "status": "needs_repair",
                "observations": [f"benchmark run failed for service {service_id}"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "payload": {
                    "server_name": server_name,
                    "preset_name": preset_name,
                    "service_id": service_id,
                },
            },
            action_kind="execute",
        )

    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"benchmark run {payload['run_id']} finished"],
            "side_effects": ["benchmark probe executed", "benchmark run recorded"],
            "payload": payload,
        },
        action_kind="execute",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.run_probe")
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--preset", required=True)
    parser.add_argument("--service-id")
    args = parser.parse_args(argv)
    result = run_probe(
        RepoPaths(root=Path.cwd()),
        server_name=args.server_name,
        preset_name=args.preset,
        service_id=args.service_id,
    )
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
