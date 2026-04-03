import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.config import RepoPaths
from tools.lib.host_access import ensure_host_ssh_access
from tools.lib.remote_types import RemoteError
from tools.lib.target_context import resolve_server_context


def bootstrap_host_ssh_access(paths: RepoPaths, server_name: str) -> dict[str, object]:
    ctx = resolve_server_context(paths, server_name)
    try:
        changed = ensure_host_ssh_access(ctx, allow_password_bootstrap=True)
    except RemoteError as exc:
        return validate_tool_result(
            {
                "status": "bootstrap_failed",
                "observations": [f"host ssh bootstrap failed for {server_name}"],
                "reason": str(exc),
                "side_effects": [],
                "retryable": True,
                "idempotent": True,
            },
            action_kind="bootstrap",
        )
    side_effects = (
        ["host authorized_keys updated", "ssh-key login verified"]
        if changed
        else ["ssh-key login verified"]
    )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"host ssh access is ready for {server_name}"],
            "side_effects": side_effects,
            "idempotent": True,
        },
        action_kind="bootstrap",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="machine.bootstrap_host_ssh")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = bootstrap_host_ssh_access(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
