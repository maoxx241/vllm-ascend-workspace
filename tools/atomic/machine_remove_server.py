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
from tools.lib.machine_registry import list_server_records, remove_server_record


def remove_server(paths: RepoPaths, server_name: str) -> dict[str, object]:
    existed = server_name in list_server_records(paths)
    remove_server_record(paths, server_name)
    observation = (
        f"removed {server_name} from server inventory"
        if existed
        else f"{server_name} was already absent from server inventory"
    )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [observation],
            "side_effects": ["servers inventory updated"],
            "payload": {"server_name": server_name, "existed": existed},
            "idempotent": True,
        },
        action_kind="execute",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="machine.remove_server")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = remove_server(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
