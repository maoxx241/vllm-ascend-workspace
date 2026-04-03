from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import RepoPaths
from .remote_types import RemoteError, TargetContext


def _read_yaml_mapping(path: Path, invalid_message: str) -> Dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RemoteError(invalid_message) from exc
    except UnicodeDecodeError as exc:
        raise RemoteError(invalid_message) from exc
    except yaml.YAMLError as exc:
        raise RemoteError(invalid_message) from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RemoteError(invalid_message)
    return loaded


def find_public_key_path() -> Path:
    ssh_dir = Path.home() / ".ssh"
    for candidate in ("id_ed25519.pub", "id_rsa.pub"):
        path = ssh_dir / candidate
        if path.is_file():
            return path
    public_keys = sorted(ssh_dir.glob("*.pub"))
    if public_keys:
        return public_keys[0]
    raise RemoteError("missing local ssh public key: expected ~/.ssh/*.pub")


def ssh_base_command(
    ctx: TargetContext,
    *,
    port: Optional[int] = None,
    batch_mode: bool = True,
    username: Optional[str] = None,
) -> List[str]:
    target_port = ctx.host.port if port is None else port
    command = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-p",
        str(target_port),
    ]
    if ctx.credential.key_path:
        command.extend(["-i", ctx.credential.key_path])
    if batch_mode:
        command.extend(["-o", "BatchMode=yes"])
    user = ctx.credential.username if username is None else username
    command.append(f"{user}@{ctx.host.host}")
    return command


def verify_host_ssh_key_login(ctx: TargetContext) -> None:
    probe = subprocess.run(
        ssh_base_command(ctx) + ["true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return
    raise RemoteError(
        f"unable to verify SSH key access to host {ctx.host.host}: "
        f"{(probe.stderr or probe.stdout or 'SSH probe failed').strip()}"
    )


def install_local_public_key_on_host(ctx: TargetContext) -> None:
    password = ctx.credential.resolved_password
    if not password:
        raise RemoteError(
            f"cannot authenticate to host {ctx.host.host}: SSH key failed and no password is configured"
        )

    try:
        import pexpect
    except ImportError as exc:
        raise RemoteError("password-based host bootstrap requires pexpect to be installed locally") from exc

    public_key_text = find_public_key_path().read_text(encoding="utf-8").strip()
    remote_script = (
        "umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
        f"grep -qxF {shlex.quote(public_key_text)} ~/.ssh/authorized_keys || "
        f"printf '%s\\n' {shlex.quote(public_key_text)} >> ~/.ssh/authorized_keys"
    )
    command = shlex.join(
        ssh_base_command(ctx, batch_mode=False) + [f"bash -lc {shlex.quote(remote_script)}"]
    )
    child = pexpect.spawn(command, encoding="utf-8", timeout=30)
    password_sent = False
    while True:
        index = child.expect(
            [
                r"(?i)are you sure you want to continue connecting",
                r"(?i)password:",
                pexpect.EOF,
                pexpect.TIMEOUT,
            ]
        )
        if index == 0:
            child.sendline("yes")
            continue
        if index == 1:
            if password_sent:
                child.close()
                raise RemoteError(f"password authentication failed for host {ctx.host.host}")
            child.sendline(password)
            password_sent = True
            continue
        if index == 2:
            child.close()
            break
        child.close()
        raise RemoteError(f"timed out while bootstrapping SSH access to host {ctx.host.host}")

    if child.exitstatus not in (0, None) and child.signalstatus is None:
        raise RemoteError(f"failed to install local SSH key on host {ctx.host.host}")


def promote_host_auth_ref_to_ssh_key(paths: RepoPaths, ctx: TargetContext) -> None:
    if ctx.credential.mode != "password" or not ctx.host.ssh_auth_ref:
        return

    auth = _read_yaml_mapping(
        paths.local_auth_file,
        "invalid auth config: .workspace.local/auth.yaml",
    )
    ssh_auth = auth.get("ssh_auth")
    if not isinstance(ssh_auth, dict):
        raise RemoteError("invalid auth config: missing ssh_auth.refs map")
    refs = ssh_auth.get("refs")
    if not isinstance(refs, dict):
        raise RemoteError("invalid auth config: missing ssh_auth.refs map")
    auth_ref = refs.get(ctx.host.ssh_auth_ref)
    if not isinstance(auth_ref, dict):
        raise RemoteError(
            f"invalid auth config: unknown ssh auth ref '{ctx.host.ssh_auth_ref}'"
        )

    promoted: Dict[str, Any] = {
        "kind": "ssh-key",
        "username": ctx.credential.username,
    }
    if ctx.credential.key_path:
        promoted["key_path"] = ctx.credential.key_path
    refs[ctx.host.ssh_auth_ref] = promoted
    paths.local_auth_file.write_text(
        yaml.safe_dump(auth, sort_keys=False),
        encoding="utf-8",
    )


def ensure_host_ssh_access(
    ctx: TargetContext,
    *,
    allow_password_bootstrap: bool = False,
) -> bool:
    probe = subprocess.run(
        ssh_base_command(ctx) + ["true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return False

    if not allow_password_bootstrap:
        detail = (probe.stderr or probe.stdout or "SSH probe failed").strip()
        if ctx.credential.mode == "password" or ctx.credential.resolved_password:
            raise RemoteError(
                f"server password bootstrap not allowed for host {ctx.host.host} in this flow: {detail}"
            )
        raise RemoteError(
            f"unable to verify SSH key access to host {ctx.host.host}: {detail}"
        )

    install_local_public_key_on_host(ctx)
    verify_host_ssh_key_login(ctx)
    return True


def run_host_command(ctx: TargetContext, script: str) -> subprocess.CompletedProcess[str]:
    ensure_host_ssh_access(ctx, allow_password_bootstrap=False)
    return subprocess.run(
        ssh_base_command(ctx) + [f"bash -lc {shlex.quote(script)}"],
        text=True,
        capture_output=True,
        check=False,
    )
