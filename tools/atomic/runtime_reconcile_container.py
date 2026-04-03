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
from tools.lib.runtime_container import ensure_host_container
from tools.lib.target_context import resolve_server_context


def reconcile_runtime_container(paths: RepoPaths, server_name: str) -> dict[str, object]:
    ctx = resolve_server_context(paths, server_name)
    try:
        reused = ensure_host_container(ctx)
    except RemoteError as exc:
        return validate_tool_result(
            {
                "status": "repair_failed",
                "observations": [f"runtime container reconcile failed for {server_name}"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": True,
            },
            action_kind="repair",
        )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"runtime container {'reused' if reused else 'created'} for {server_name}"],
            "side_effects": ["runtime container reconciled"],
            "idempotent": True,
        },
        action_kind="repair",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime.reconcile_container")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = reconcile_runtime_container(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
