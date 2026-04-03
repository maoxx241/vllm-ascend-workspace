from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.benchmark_execution import describe_benchmark_preset


def describe_preset(preset_name: str) -> dict[str, object]:
    try:
        payload = describe_benchmark_preset(preset_name)
    except RuntimeError as exc:
        return validate_tool_result(
            {
                "status": "needs_repair",
                "observations": [f"benchmark preset {preset_name} is unavailable"],
                "reason": str(exc),
                "idempotent": True,
                "payload": {"preset_name": preset_name},
            },
            action_kind="probe",
        )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"benchmark preset {preset_name} loaded"],
            "idempotent": True,
            "payload": payload,
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.describe_preset")
    parser.add_argument("--preset", required=True)
    args = parser.parse_args(argv)
    result = describe_preset(args.preset)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
