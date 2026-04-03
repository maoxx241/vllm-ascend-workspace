import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.config import RepoPaths
from tools.lib.host_access import verify_host_ssh_key_login
from tools.lib.remote_types import RemoteError
from tools.lib.target_context import resolve_server_context


def probe_host_ssh(paths: RepoPaths, server_name: str) -> dict[str, object]:
    ctx = resolve_server_context(paths, server_name)
    try:
        verify_host_ssh_key_login(ctx)
    except RemoteError as exc:
        return validate_tool_result(
            {
                "status": "needs_repair",
                "observations": [f"host ssh key login failed for {server_name}"],
                "reason": str(exc),
                "next_probes": ["machine.probe_host_ssh"],
                "idempotent": True,
            },
            action_kind="probe",
        )
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"host ssh key login succeeded for {server_name}"],
            "idempotent": True,
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="machine.probe_host_ssh")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = probe_host_ssh(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
