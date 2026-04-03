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
from tools.lib.runtime_cleanup import destroy_host_runtime
from tools.lib.target_context import resolve_server_context


def cleanup_runtime_server(paths: RepoPaths, server_name: str) -> dict[str, object]:
    ctx = resolve_server_context(paths, server_name)
    try:
        destroy_host_runtime(ctx)
    except RemoteError as exc:
        return validate_tool_result(
            {
                "status": "cleanup_failed",
                "observations": [f"runtime cleanup failed for {server_name}"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": True,
            },
            action_kind="cleanup",
        )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"runtime removed for {server_name}"],
            "side_effects": ["remote container removed", "remote workspace mirror removed"],
            "idempotent": True,
        },
        action_kind="cleanup",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime.cleanup_server")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = cleanup_runtime_server(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
