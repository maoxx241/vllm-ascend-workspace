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
from tools.lib.serving_session import list_service_sessions


def list_services(paths: RepoPaths) -> dict[str, object]:
    services = list_service_sessions(paths)
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"{len(services)} service session(s) recorded"],
            "idempotent": True,
            "payload": {"services": services},
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    argparse.ArgumentParser(prog="serving.list_sessions").parse_args(argv)
    result = list_services(RepoPaths(root=Path.cwd()))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
