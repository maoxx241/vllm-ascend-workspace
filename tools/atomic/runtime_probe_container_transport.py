import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.config import RepoPaths
from tools.lib.runtime_transport import probe_container_ssh_transport, probe_docker_exec_transport
from tools.lib.target_context import resolve_server_context


def probe_container_transport(paths: RepoPaths, server_name: str) -> dict[str, object]:
    ctx = resolve_server_context(paths, server_name)
    ssh_status, ssh_detail = probe_container_ssh_transport(ctx)
    if ssh_status == "ready":
        return validate_tool_result(
            {
                "status": "ready",
                "observations": [ssh_detail],
                "idempotent": True,
            },
            action_kind="probe",
        )

    docker_status, docker_detail = probe_docker_exec_transport(ctx)
    if docker_status == "ready":
        return validate_tool_result(
            {
                "status": "ready",
                "observations": [ssh_detail, docker_detail],
                "reason": "container ssh unavailable; docker exec fallback is available",
                "next_probes": ["runtime.bootstrap_container_transport"],
                "idempotent": True,
            },
            action_kind="probe",
        )

    return validate_tool_result(
        {
            "status": "needs_repair",
            "observations": [ssh_detail, docker_detail],
            "reason": "no runtime transport is ready",
            "next_probes": ["runtime.bootstrap_container_transport"],
            "idempotent": True,
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime.probe_container_transport")
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    result = probe_container_transport(RepoPaths(root=Path.cwd()), args.server_name)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
