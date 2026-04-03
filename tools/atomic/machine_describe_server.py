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
from tools.lib.machine_registry import describe_server_record


def describe_server(paths: RepoPaths, server_name: str) -> dict[str, object]:
    server = describe_server_record(paths, server_name)
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"loaded inventory for {server_name}"],
            "payload": {"server_name": server_name, "server": server},
            "idempotent": True,
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="machine.describe_server")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = describe_server(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
