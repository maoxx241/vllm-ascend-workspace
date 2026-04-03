from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any, Dict

from .host_access import run_host_command, ssh_base_command
from .remote_types import RemoteError, TargetContext


def ensure_host_path(ctx: TargetContext) -> None:
    result = run_host_command(ctx, f"mkdir -p {shlex.quote(ctx.runtime.host_workspace_path)}")
    if result.returncode != 0:
        raise RemoteError(
            f"failed to prepare host workspace path: {(result.stderr or result.stdout).strip()}"
        )


def sync_workspace_mirror(root: str, ctx: TargetContext) -> None:
    ensure_host_path(ctx)
    tar_process = subprocess.Popen(
        [
            "tar",
            "--exclude=.workspace.local",
            "--exclude=.pytest_cache",
            "--exclude=__pycache__",
            "-cf",
            "-",
            ".",
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ssh_process = subprocess.Popen(
        ssh_base_command(ctx) + [f"tar -xf - -C {shlex.quote(ctx.runtime.host_workspace_path)}"],
        stdin=tar_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert tar_process.stdout is not None
    tar_process.stdout.close()
    _, ssh_stderr = ssh_process.communicate()
    _, tar_stderr = tar_process.communicate()
    if tar_process.returncode != 0:
        raise RemoteError(
            f"failed to archive control repo for host sync: {tar_stderr.decode().strip()}"
        )
    if ssh_process.returncode != 0:
        raise RemoteError(
            f"failed to stream control repo to host: {ssh_stderr.decode().strip()}"
        )


def run_docker_exec(ctx: TargetContext, script: str) -> subprocess.CompletedProcess[str]:
    docker_script = f"docker exec -i {shlex.quote(ctx.runtime.container_name)} bash -lc {shlex.quote(script)}"
    return run_host_command(ctx, docker_script)


def docker_run_command(ctx: TargetContext) -> str:
    mount_spec = f"{ctx.runtime.host_workspace_path}:{ctx.runtime.workspace_root}/workspace"
    base_args = [
        "docker",
        "run",
        "-d",
        "--restart",
        "unless-stopped",
        "--network",
        "host",
        "--privileged",
        "--name",
        ctx.runtime.container_name,
        "-v",
        mount_spec,
        "-w",
        f"{ctx.runtime.workspace_root}/workspace",
    ]
    base_args.extend(ctx.runtime.docker_run_args)
    base_args.extend([ctx.runtime.image_ref, "bash", "-lc", "while true; do sleep 3600; done"])
    return shlex.join(base_args)


def inspect_container_contract(ctx: TargetContext) -> Dict[str, Any]:
    inspect_result = run_host_command(
        ctx,
        f"docker inspect {shlex.quote(ctx.runtime.container_name)}",
    )
    if inspect_result.returncode != 0:
        raise RemoteError(
            f"failed to inspect container '{ctx.runtime.container_name}': "
            f"{(inspect_result.stderr or inspect_result.stdout).strip()}"
        )
    try:
        payload = json.loads(inspect_result.stdout or "[]")[0]
    except (IndexError, json.JSONDecodeError) as exc:
        raise RemoteError(f"invalid inspect payload for container '{ctx.runtime.container_name}'") from exc
    mounts = sorted(
        f"{mount.get('Source', '')}:{mount.get('Destination', '')}"
        for mount in payload.get("Mounts", [])
        if isinstance(mount, dict)
    )
    sshd_config = ""
    state = payload.get("State")
    if isinstance(state, dict) and state.get("Running") is True:
        sshd_probe = run_docker_exec(ctx, "cat /etc/ssh/sshd_config 2>/dev/null || true")
        if sshd_probe.returncode == 0:
            sshd_config = sshd_probe.stdout or ""
    return {
        "image": payload.get("Config", {}).get("Image"),
        "network_mode": payload.get("HostConfig", {}).get("NetworkMode"),
        "mounts": mounts,
        "sshd_config": sshd_config,
    }


def container_requires_rebuild(ctx: TargetContext) -> bool:
    contract = inspect_container_contract(ctx)
    expected_mount = f"{ctx.runtime.host_workspace_path}:{ctx.runtime.workspace_root}/workspace"
    if contract.get("image") != ctx.runtime.image_ref:
        return True
    if contract.get("network_mode") != "host":
        return True
    mounts = contract.get("mounts")
    if not isinstance(mounts, list) or expected_mount not in mounts:
        return True
    sshd_config = str(contract.get("sshd_config", "")).lower()
    required_sshd_lines = (
        f"port {ctx.runtime.ssh_port}".lower(),
        "permitrootlogin prohibit-password",
        "passwordauthentication no",
        "kbdinteractiveauthentication no",
    )
    return not sshd_config or not all(line in sshd_config for line in required_sshd_lines)


def remove_host_container(ctx: TargetContext) -> None:
    remove = run_host_command(
        ctx,
        f"docker rm -f {shlex.quote(ctx.runtime.container_name)} >/dev/null",
    )
    if remove.returncode != 0:
        raise RemoteError(
            f"failed to remove container '{ctx.runtime.container_name}': "
            f"{(remove.stderr or remove.stdout).strip()}"
        )


def ensure_host_container(ctx: TargetContext) -> bool:
    inspect = run_host_command(
        ctx,
        f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(ctx.runtime.container_name)} 2>/dev/null || true",
    )
    running_state = (inspect.stdout or "").strip()
    if running_state == "true":
        if container_requires_rebuild(ctx):
            remove_host_container(ctx)
        else:
            return True
    elif running_state == "false":
        start = run_host_command(
            ctx,
            f"docker start {shlex.quote(ctx.runtime.container_name)} >/dev/null",
        )
        if start.returncode != 0:
            raise RemoteError(
                f"failed to start existing container '{ctx.runtime.container_name}': "
                f"{(start.stderr or start.stdout).strip()}"
            )
        if container_requires_rebuild(ctx):
            remove_host_container(ctx)
        else:
            return True
    create = run_host_command(ctx, docker_run_command(ctx))
    if create.returncode != 0:
        raise RemoteError(
            f"failed to create container '{ctx.runtime.container_name}': "
            f"{(create.stderr or create.stdout).strip()}"
        )
    return False
