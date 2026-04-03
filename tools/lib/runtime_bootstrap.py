from __future__ import annotations

import random
import re
import subprocess
from typing import Any, Dict

from .config import RepoPaths
from .host_access import ensure_host_ssh_access, promote_host_auth_ref_to_ssh_key
from .remote_types import RemoteError, TargetContext, VerificationCheck, VerificationResult
from .runtime_container import ensure_host_container, sync_workspace_mirror
from .target_context import resolve_server_context
from .runtime_transport import (
    bootstrap_container_runtime,
    probe_container_ssh_transport,
    probe_docker_exec_transport,
    write_host_runtime_state,
)

RUNTIME_PORT_MIN = 40000
RUNTIME_PORT_MAX = 54999


def _list_listening_ports() -> set[int]:
    commands = (
        ["ss", "-H", "-tln"],
        ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
    )
    port_pattern = re.compile(r":(\d+)\b")
    for command in commands:
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            continue
        if result.returncode != 0:
            continue
        ports = {
            int(match.group(1))
            for match in port_pattern.finditer(result.stdout or "")
        }
        if ports:
            return ports
    return set()


def allocate_runtime_ssh_port(occupied: set[int] | None = None) -> int:
    occupied_ports = set(_list_listening_ports() if occupied is None else occupied)
    for _ in range(32):
        candidate = random.randint(RUNTIME_PORT_MIN, RUNTIME_PORT_MAX)
        if candidate not in occupied_ports:
            return candidate
    raise RemoteError("unable to allocate runtime ssh port")


def _runtime_mapping(
    ctx: TargetContext,
    *,
    transport: str = "container-ssh",
    container_reused: bool = True,
) -> Dict[str, Any]:
    if transport == "container-ssh":
        endpoint = f"ssh://root@{ctx.host.host}:{ctx.runtime.ssh_port}"
    else:
        endpoint = f"docker-exec://{ctx.credential.username}@{ctx.host.host}/{ctx.runtime.container_name}"
    return {
        "image_ref": ctx.runtime.image_ref,
        "container_name": ctx.runtime.container_name,
        "ssh_port": ctx.runtime.ssh_port,
        "workspace_root": ctx.runtime.workspace_root,
        "bootstrap_mode": ctx.runtime.bootstrap_mode,
        "transport": transport,
        "container_endpoint": endpoint,
        "host_name": ctx.host.name,
        "host": ctx.host.host,
        "host_port": ctx.host.port,
        "login_user": ctx.host.login_user,
        "host_workspace_path": ctx.runtime.host_workspace_path,
        "reused": container_reused,
    }


def bootstrap_runtime(paths: RepoPaths, ctx: TargetContext) -> Dict[str, Any]:
    ensure_host_ssh_access(ctx, allow_password_bootstrap=True)
    promote_host_auth_ref_to_ssh_key(paths, ctx)
    sync_workspace_mirror(str(paths.root), ctx)
    reused = ensure_host_container(ctx)
    transport = bootstrap_container_runtime(ctx)
    write_host_runtime_state(ctx, transport)
    return _runtime_mapping(ctx, transport=transport, container_reused=reused)


def probe_runtime(paths: RepoPaths, ctx: TargetContext) -> VerificationResult:
    container_ssh_status, container_ssh_detail = probe_container_ssh_transport(ctx)
    container_ssh_check = VerificationCheck(
        name="container_ssh",
        status=container_ssh_status,
        detail=container_ssh_detail,
    )
    if container_ssh_status == "ready":
        return VerificationResult.ready(
            summary=f"runtime ready for host {ctx.host.host} via container ssh",
            runtime=_runtime_mapping(ctx, transport="container-ssh"),
            checks=[container_ssh_check],
        )

    docker_exec_status, docker_exec_detail = probe_docker_exec_transport(ctx)
    docker_exec_check = VerificationCheck(
        name="docker_exec",
        status="available" if docker_exec_status == "ready" else docker_exec_status,
        detail=(
            "repair-only transport available via docker exec"
            if docker_exec_status == "ready"
            else docker_exec_detail
        ),
    )
    return VerificationResult.needs_repair(
        summary=f"container SSH is not ready for host {ctx.host.host}",
        runtime=_runtime_mapping(ctx, transport="container-ssh"),
        checks=[container_ssh_check, docker_exec_check],
    )


verify_runtime = probe_runtime


def bootstrap_runtime_for_server(paths: RepoPaths, server_name: str) -> Dict[str, Any]:
    return bootstrap_runtime(paths, resolve_server_context(paths, server_name))


def probe_runtime_for_server(paths: RepoPaths, server_name: str) -> VerificationResult:
    return probe_runtime(paths, resolve_server_context(paths, server_name))
