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
from tools.lib.machine_registry import list_server_records


def list_servers(paths: RepoPaths) -> dict[str, object]:
    servers = list_server_records(paths)
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"loaded {len(servers)} server inventory record(s)"],
            "payload": {"servers": servers},
            "idempotent": True,
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="machine.list_servers")
    parser.parse_args(argv)
    result = list_servers(RepoPaths(root=Path.cwd()))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
