from __future__ import annotations

from typing import Any

from .config import RepoPaths
from .machine_registry import (
    build_server_record,
    list_server_records,
    record_verification_observation,
    remove_server_record,
    update_server_status,
    upsert_server_record,
    verification_status,
)
from .runtime_bootstrap import probe_runtime_for_server
from .runtime_cleanup import cleanup_runtime_for_server
from .vaws_compat import unexpected_subcommand

DEFAULT_SERVER_PORT = 22
DEFAULT_RUNTIME_IMAGE = "quay.nju.edu.cn/ascend/vllm-ascend:latest"
DEFAULT_RUNTIME_CONTAINER = "vaws-workspace"
DEFAULT_RUNTIME_SSH_PORT = 63269
DEFAULT_RUNTIME_WORKSPACE_ROOT = "/vllm-workspace"
DEFAULT_RUNTIME_BOOTSTRAP_MODE = "host-then-container"
DEFAULT_HOST_WORKSPACE_BASE = "/root/.vaws/targets"
SUCCESSFUL_CLEANUP_STATUSES = {"ready", "removed", "already_absent"}


def _server_record(
    server_name: str,
    server_host: str,
    *,
    ssh_auth_ref: str,
    server_user: str,
    server_port: int,
    runtime_image: str,
    runtime_container: str,
    runtime_ssh_port: int,
    runtime_workspace_root: str,
    runtime_bootstrap_mode: str,
) -> dict[str, object]:
    return build_server_record(
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


def list_machines(paths: RepoPaths) -> int:
    servers = list_server_records(paths)
    print("machine list:")
    if not servers:
        print("  (no servers)")
        return 0
    for server_name, server in sorted(servers.items()):
        if not isinstance(server, dict):
            continue
        runtime = server.get("runtime")
        container_name = runtime.get("container_name") if isinstance(runtime, dict) else "?"
        print(f"- {server_name}: {server.get('host', '?')}:{server.get('port', '?')} {container_name}")
    return 0


def verify_machine(paths: RepoPaths, server_name: str) -> int:
    verification = probe_runtime_for_server(paths, server_name)
    status = verification_status(verification)
    update_server_status(paths, server_name, status)
    record_verification_observation(paths, server_name, verification)

    if status != "ready":
        print(f"machine verify: {status} ({server_name})")
        return 1
    print(f"machine verify: ready ({server_name})")
    return 0


def add_machine(
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
) -> int:
    upsert_server_record(
        paths,
        server_name,
        _server_record(
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
        ),
    )
    print(f"machine add: recorded ({server_name})")
    return 0


def remove_machine(paths: RepoPaths, server_name: str) -> int:
    partial_cleanup = False
    try:
        cleanup = cleanup_runtime_for_server(paths, server_name)
        cleanup_status = getattr(cleanup, "status", "ready")
        cleanup_detail = getattr(cleanup, "detail", "")
        if cleanup_status not in SUCCESSFUL_CLEANUP_STATUSES:
            partial_cleanup = True
            print(f"machine remove: partial cleanup for {server_name}: {cleanup_detail}")
    except Exception as exc:  # noqa: BLE001
        partial_cleanup = True
        print(f"machine remove: partial cleanup for {server_name}: {exc}")

    remove_server_record(paths, server_name)

    if partial_cleanup:
        return 1
    print(f"machine remove: ok ({server_name})")
    return 0


def run(paths: RepoPaths, args: Any) -> int:
    if args.machine_command == "list":
        return list_machines(paths)
    if args.machine_command == "add":
        return add_machine(
            paths,
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
    if args.machine_command == "verify":
        return verify_machine(paths, args.server_name)
    if args.machine_command == "remove":
        return remove_machine(paths, args.server_name)
    raise unexpected_subcommand("machine", args.machine_command)
