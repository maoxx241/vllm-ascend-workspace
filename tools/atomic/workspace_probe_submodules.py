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
from tools.lib.repo_topology import probe_submodules


def probe_submodules_tool(paths: RepoPaths) -> dict[str, object]:
    payload = probe_submodules(paths)
    result = {
        "status": payload["status"],
        "observations": [str(payload["detail"])],
        "payload": payload,
        "idempotent": True,
    }
    if payload["status"] != "ready":
        result["reason"] = str(payload["detail"])
        result["next_probes"] = ["workspace.probe_submodules"]
    return validate_tool_result(result, action_kind="probe")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="workspace.probe_submodules")
    parser.parse_args(argv)
    result = probe_submodules_tool(RepoPaths(root=Path.cwd()))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
