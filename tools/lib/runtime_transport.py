from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .host_access import find_public_key_path, ssh_base_command
from .remote_types import RemoteError, TargetContext
from .runtime_container import run_docker_exec


def probe_container_ssh_transport(ctx: TargetContext) -> tuple[str, str]:
    probe = subprocess.run(
        ssh_base_command(ctx, port=ctx.runtime.ssh_port, username="root") + ["true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return "ready", "container SSH probe succeeded"
    return "needs_repair", (probe.stderr or probe.stdout or "container SSH probe failed").strip()


def probe_docker_exec_transport(ctx: TargetContext) -> tuple[str, str]:
    try:
        probe = run_docker_exec(ctx, "true")
    except RemoteError as exc:
        return "needs_repair", str(exc)
    if probe.returncode == 0:
        return "ready", "docker exec probe succeeded"
    return "needs_repair", (probe.stderr or probe.stdout or "docker exec probe failed").strip()


def probe_container_ssh(ctx: TargetContext) -> bool:
    status, _detail = probe_container_ssh_transport(ctx)
    return status == "ready"


def _format_remote_failure(result: subprocess.CompletedProcess[str]) -> str:
    parts = [f"rc={result.returncode}"]
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    if stderr:
        parts.append(f"stderr={stderr}")
    if stdout:
        parts.append(f"stdout={stdout}")
    return "; ".join(parts)


def bootstrap_container_runtime(ctx: TargetContext) -> str:
    public_key = find_public_key_path().read_text(encoding="utf-8").strip()
    bootstrap_script = "\n".join(
        [
            "set -euo pipefail",
            f"mkdir -p {shlex.quote(ctx.runtime.workspace_root)}",
            f"mkdir -p {shlex.quote(ctx.runtime.workspace_root + '/.vaws/targets')}",
            f"git config --global --add safe.directory {shlex.quote(ctx.runtime.workspace_root + '/workspace')}",
            f"git config --global --add safe.directory {shlex.quote(ctx.runtime.workspace_root + '/workspace/vllm')}",
            f"git config --global --add safe.directory {shlex.quote(ctx.runtime.workspace_root + '/workspace/vllm-ascend')}",
            "apt-get -o Acquire::Check-Date=false -o Acquire::Check-Valid-Until=false update",
            "DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-missing openssh-server",
            "DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-missing openssh-client",
            "DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-missing ssh",
            "mkdir -p /root/.ssh",
            "chmod 700 /root/.ssh",
            "touch /root/.ssh/authorized_keys",
            "chmod 600 /root/.ssh/authorized_keys",
            f"grep -qxF {shlex.quote(public_key)} /root/.ssh/authorized_keys || printf '%s\\n' {shlex.quote(public_key)} >> /root/.ssh/authorized_keys",
            "ssh-keygen -A",
            "if [ ! -f /root/.ssh/id_ed25519 ]; then ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519 >/dev/null; fi",
            "mkdir -p /run/sshd /var/run/sshd",
            "cat > /etc/ssh/sshd_config <<'EOF'",
            f"Port {ctx.runtime.ssh_port}",
            "PermitRootLogin prohibit-password",
            "PubkeyAuthentication yes",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "ChallengeResponseAuthentication no",
            "UsePAM yes",
            "PidFile /var/run/sshd.pid",
            "AuthorizedKeysFile .ssh/authorized_keys",
            "EOF",
            f"pgrep -a -x sshd | grep ' -p {ctx.runtime.ssh_port}' | awk '{{print $1}}' | xargs -r kill 2>/dev/null || true",
            "/usr/sbin/sshd -t",
            f"/usr/sbin/sshd -p {ctx.runtime.ssh_port}",
        ]
    )
    result = run_docker_exec(ctx, bootstrap_script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to bootstrap runtime inside container: {_format_remote_failure(result)}"
        )
    if probe_container_ssh(ctx):
        return "container-ssh"
    return "docker-exec"


def run_container_command(ctx: TargetContext, transport: str, script: str) -> subprocess.CompletedProcess[str]:
    if transport == "container-ssh":
        return subprocess.run(
            ssh_base_command(ctx, port=ctx.runtime.ssh_port, username="root")
            + [f"bash -lc {shlex.quote(script)}"],
            text=True,
            capture_output=True,
            check=False,
        )
    return run_docker_exec(ctx, script)


def run_detached_container_command(
    ctx: TargetContext,
    transport: str,
    command: str,
    *,
    log_path: str,
    pid_path: str,
) -> subprocess.CompletedProcess[str]:
    wrapped = "\n".join(
        [
            "set -euo pipefail",
            f"mkdir -p {shlex.quote(str(Path(log_path).parent))}",
            f"nohup bash -lc {shlex.quote(command)} > {shlex.quote(log_path)} 2>&1 < /dev/null &",
            f"echo $! > {shlex.quote(pid_path)}",
            f"cat {shlex.quote(pid_path)}",
        ]
    )
    return run_container_command(ctx, transport, wrapped)


def resolve_available_runtime_transport(ctx: TargetContext) -> str:
    ssh_status, ssh_detail = probe_container_ssh_transport(ctx)
    if ssh_status == "ready":
        return "container-ssh"

    docker_status, docker_detail = probe_docker_exec_transport(ctx)
    if docker_status == "ready":
        return "docker-exec"

    raise RemoteError(f"no runtime transport is ready: {ssh_detail}; {docker_detail}")


def write_host_runtime_state(ctx: TargetContext, transport: str) -> None:
    runtime_state = {
        "target": ctx.name,
        "workspace_root": ctx.runtime.workspace_root,
        "control_repo_path": f"{ctx.runtime.workspace_root}/workspace",
        "container": {
            "name": ctx.runtime.container_name,
            "created": True,
            "reused": True,
        },
        "endpoint": {
            "transport": transport,
            "host": ctx.host.host,
            "port": ctx.runtime.ssh_port,
        },
    }
    script = "\n".join(
        [
            "set -e",
            f"mkdir -p {shlex.quote(ctx.runtime.workspace_root + '/.vaws')}",
            f"cat > {shlex.quote(ctx.runtime.workspace_root + '/.vaws/runtime.json')} <<'EOF'",
            json.dumps(runtime_state, indent=2),
            "EOF",
        ]
    )
    result = run_container_command(ctx, transport, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to record runtime state in container: {(result.stderr or result.stdout).strip()}"
        )
