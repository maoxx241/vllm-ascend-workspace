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
from tools.lib.machine_registry import build_server_record, upsert_server_record

DEFAULT_SERVER_PORT = 22
DEFAULT_RUNTIME_IMAGE = "quay.nju.edu.cn/ascend/vllm-ascend:latest"
DEFAULT_RUNTIME_CONTAINER = "vaws-workspace"
DEFAULT_RUNTIME_SSH_PORT = 63269
DEFAULT_RUNTIME_WORKSPACE_ROOT = "/vllm-workspace"
DEFAULT_RUNTIME_BOOTSTRAP_MODE = "host-then-container"
DEFAULT_HOST_WORKSPACE_BASE = "/root/.vaws/targets"


def register_server(
    paths: RepoPaths,
    server_name: str,
    server_host: str,
    *,
    ssh_auth_ref: str,
    server_user: str = "root",
    server_port: int = DEFAULT_SERVER_PORT,
    runtime_image: str = DEFAULT_RUNTIME_IMAGE,
    runtime_container: str = DEFAULT_RUNTIME_CONTAINER,
    runtime_ssh_port: int = DEFAULT_RUNTIME_SSH_PORT,
    runtime_workspace_root: str = DEFAULT_RUNTIME_WORKSPACE_ROOT,
    runtime_bootstrap_mode: str = DEFAULT_RUNTIME_BOOTSTRAP_MODE,
) -> dict[str, object]:
    record = build_server_record(
        server_name,
        server_host,
        ssh_auth_ref=ssh_auth_ref,
        server_user=server_user,
        server_port=server_port,
        runtime_image=runtime_image,
        runtime_container=runtime_container,
        runtime_ssh_port=runtime_ssh_port,
        runtime_workspace_root=runtime_workspace_root,
        runtime_bootstrap_mode=runtime_bootstrap_mode,
        host_workspace_base=DEFAULT_HOST_WORKSPACE_BASE,
    )
    upsert_server_record(paths, server_name, record)
    return validate_tool_result(
        {
            "status": "ready",
            "observations": [f"registered server inventory for {server_name}"],
            "side_effects": ["servers inventory updated"],
            "payload": {"server_name": server_name, "server": record},
            "idempotent": True,
        },
        action_kind="execute",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="machine.register_server")
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--server-host", required=True)
    parser.add_argument("--ssh-auth-ref", required=True)
    parser.add_argument("--server-user", default="root")
    parser.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
    parser.add_argument("--runtime-image", default=DEFAULT_RUNTIME_IMAGE)
    parser.add_argument("--runtime-container", default=DEFAULT_RUNTIME_CONTAINER)
    parser.add_argument("--runtime-ssh-port", type=int, default=DEFAULT_RUNTIME_SSH_PORT)
    parser.add_argument("--runtime-workspace-root", default=DEFAULT_RUNTIME_WORKSPACE_ROOT)
    parser.add_argument("--runtime-bootstrap-mode", default=DEFAULT_RUNTIME_BOOTSTRAP_MODE)
    args = parser.parse_args(argv)
    result = register_server(
        RepoPaths(root=Path.cwd()),
        args.server_name,
        args.server_host,
        ssh_auth_ref=args.ssh_auth_ref,
        server_user=args.server_user,
        server_port=args.server_port,
        runtime_image=args.runtime_image,
        runtime_container=args.runtime_container,
        runtime_ssh_port=args.runtime_ssh_port,
        runtime_workspace_root=args.runtime_workspace_root,
        runtime_bootstrap_mode=args.runtime_bootstrap_mode,
    )
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
