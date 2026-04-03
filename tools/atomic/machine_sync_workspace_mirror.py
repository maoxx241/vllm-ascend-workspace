import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.config import RepoPaths
from tools.lib.remote_types import RemoteError
from tools.lib.runtime_container import sync_workspace_mirror
from tools.lib.target_context import resolve_server_context


def sync_machine_workspace(paths: RepoPaths, server_name: str) -> dict[str, object]:
    ctx = resolve_server_context(paths, server_name)
    try:
        sync_workspace_mirror(str(paths.root), ctx)
    except RemoteError as exc:
        return validate_tool_result(
            {
                "status": "bootstrap_failed",
                "observations": [f"workspace mirror sync failed for {server_name}"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": True,
            },
            action_kind="bootstrap",
        )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"workspace mirror synced for {server_name}"],
            "side_effects": ["host workspace mirror updated"],
            "idempotent": True,
        },
        action_kind="bootstrap",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="machine.sync_workspace_mirror")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = sync_machine_workspace(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
