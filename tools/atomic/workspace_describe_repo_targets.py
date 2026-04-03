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
from tools.lib.repo_targets import describe_repo_targets


def describe_repo_targets_tool(paths: RepoPaths) -> dict[str, object]:
    payload = describe_repo_targets(paths)
    return validate_tool_result(
        {
            "status": "ready",
            "observations": ["resolved current workspace, vllm, and vllm-ascend git targets"],
            "payload": payload,
            "idempotent": True,
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="workspace.describe_repo_targets")
    parser.parse_args(argv)
    result = describe_repo_targets_tool(RepoPaths(root=Path.cwd()))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
