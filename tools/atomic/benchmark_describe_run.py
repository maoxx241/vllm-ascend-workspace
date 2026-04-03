from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.benchmark_execution import describe_benchmark_run
from tools.lib.config import RepoPaths


def describe_run(paths: RepoPaths, run_id: str | None) -> dict[str, object]:
    try:
        payload = describe_benchmark_run(paths, run_id)
    except RuntimeError as exc:
        return validate_tool_result(
            {
                "status": "needs_repair",
                "observations": ["benchmark run is unavailable"],
                "reason": str(exc),
                "idempotent": True,
                "payload": {"run_id": run_id},
            },
            action_kind="probe",
        )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"benchmark run {payload['run_id']} loaded"],
            "idempotent": True,
            "payload": payload,
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark.describe_run")
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    result = describe_run(RepoPaths(root=Path.cwd()), args.run_id)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
