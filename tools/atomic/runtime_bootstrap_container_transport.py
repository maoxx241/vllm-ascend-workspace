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
from tools.lib.runtime_transport import bootstrap_container_runtime, write_host_runtime_state
from tools.lib.target_context import resolve_server_context


def bootstrap_runtime_transport(paths: RepoPaths, server_name: str) -> dict[str, object]:
    ctx = resolve_server_context(paths, server_name)
    try:
        transport = bootstrap_container_runtime(ctx)
        write_host_runtime_state(ctx, transport)
    except RemoteError as exc:
        return validate_tool_result(
            {
                "status": "bootstrap_failed",
                "observations": [f"runtime transport bootstrap failed for {server_name}"],
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
            "observations": [f"runtime transport {transport} ready for {server_name}"],
            "side_effects": ["container sshd configured", "runtime state recorded"],
            "idempotent": True,
        },
        action_kind="bootstrap",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime.bootstrap_container_transport")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = bootstrap_runtime_transport(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
