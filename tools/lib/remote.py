from __future__ import annotations

import json
import os
import random
import re
import shlex
import subprocess
import shutil
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .capability_state import read_capability_state, write_capability_leaf, write_capability_state
from .config import RepoPaths

DEFAULT_HOST_WORKSPACE_BASE = "/root/.vaws/targets"
DEFAULT_WORKSPACE_ROOT = "/vllm-workspace"
RUNTIME_PORT_MIN = 40000
RUNTIME_PORT_MAX = 54999


class RemoteError(RuntimeError):
    """Remote bootstrap or runtime materialization failure."""
@dataclass(frozen=True)
class CredentialGroup:
    mode: str
    username: str
    password: Optional[str] = None
    password_env: Optional[str] = None
    key_path: Optional[str] = None
    token_env: Optional[str] = None
    simulation_root: Optional[Path] = None

    @property
    def resolved_password(self) -> Optional[str]:
        if self.password:
            return self.password
        if self.password_env:
            value = os.environ.get(self.password_env)
            if value:
                return value
        return None


@dataclass(frozen=True)
class HostSpec:
    name: str
    host: str
    port: int
    login_user: str
    auth_group: str
    ssh_auth_ref: Optional[str] = None


@dataclass(frozen=True)
class RuntimeSpec:
    image_ref: str
    container_name: str
    ssh_port: int
    workspace_root: str
    bootstrap_mode: str
    host_workspace_path: str
    docker_run_args: List[str]


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    status: str
    detail: Optional[str] = None

    def to_mapping(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class VerificationResult:
    status: str
    summary: str
    checks: List[VerificationCheck]
    runtime: Dict[str, Any]

    @classmethod
    def ready(
        cls,
        *,
        summary: str,
        runtime: Dict[str, Any],
        checks: Optional[List[VerificationCheck]] = None,
    ) -> "VerificationResult":
        return cls(
            status="ready",
            summary=summary,
            checks=list(checks or []),
            runtime=dict(runtime),
        )

    @classmethod
    def needs_repair(
        cls,
        *,
        summary: str,
        runtime: Dict[str, Any],
        checks: List[VerificationCheck],
    ) -> "VerificationResult":
        return cls(
            status="needs_repair",
            summary=summary,
            checks=list(checks),
            runtime=dict(runtime),
        )

    def to_mapping(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": self.status,
            "summary": self.summary,
            "checks": [check.to_mapping() for check in self.checks],
            "runtime": dict(self.runtime),
        }
        return payload


@dataclass(frozen=True)
class CleanupResult:
    server_name: str
    status: str
    detail: str

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "server_name": self.server_name,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TargetContext:
    name: str
    host: HostSpec
    credential: CredentialGroup
    runtime: RuntimeSpec


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


def allocate_runtime_ssh_port(occupied: Optional[set[int]] = None) -> int:
    occupied_ports = set(_list_listening_ports() if occupied is None else occupied)
    for _ in range(32):
        candidate = random.randint(RUNTIME_PORT_MIN, RUNTIME_PORT_MAX)
        if candidate not in occupied_ports:
            return candidate
    raise RemoteError("unable to allocate runtime ssh port")


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


def _require_non_empty_string(value: Any, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemoteError(message)
    return value.strip()


def _require_int_in_range(value: Any, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 65535:
        raise RemoteError(message)
    return value
def _load_auth_config(paths: RepoPaths) -> Dict[str, Any]:
    return _read_yaml_mapping(
        paths.local_overlay / "auth.yaml",
        "invalid auth config: .workspace.local/auth.yaml",
    )


def _load_servers_config(paths: RepoPaths) -> Dict[str, Any]:
    return _read_yaml_mapping(
        paths.local_servers_file,
        "invalid server config: .workspace.local/servers.yaml",
    )
def _host_ssh_auth_ref(host_config: Dict[str, Any]) -> Optional[str]:
    auth_ref = host_config.get("ssh_auth_ref")
    if isinstance(auth_ref, str) and auth_ref.strip():
        return auth_ref.strip()
    return None


def _modern_auth_ref_names(auth: Dict[str, Any]) -> List[str]:
    ssh_auth = auth.get("ssh_auth")
    if not isinstance(ssh_auth, dict):
        return []

    refs = ssh_auth.get("refs")
    if not isinstance(refs, dict):
        return []

    return sorted(
        ref_name.strip()
        for ref_name in refs
        if isinstance(ref_name, str) and ref_name.strip()
    )
def _credential_group_from_ref(
    ref: Dict[str, Any],
    ref_name: str,
    fallback_username: str,
) -> CredentialGroup:
    kind = _require_non_empty_string(
        ref.get("kind"),
        f"invalid auth config: ssh_auth ref '{ref_name}' missing kind",
    )
    username = ref.get("username", fallback_username)
    if not isinstance(username, str) or not username.strip():
        raise RemoteError(
            f"invalid auth config: ssh_auth ref '{ref_name}' missing username"
        )

    simulation_root = ref.get("simulation_root")
    simulation_root_path: Optional[Path] = None
    if kind == "local-simulation":
        if not isinstance(simulation_root, str) or not simulation_root.strip():
            raise RemoteError(
                f"invalid auth config: ssh_auth ref '{ref_name}' missing simulation_root"
            )
        simulation_root_path = Path(simulation_root)

    return CredentialGroup(
        mode=kind.strip(),
        username=username.strip(),
        password=ref.get("password"),
        password_env=ref.get("password_env"),
        key_path=ref.get("key_path"),
        token_env=ref.get("token_env"),
        simulation_root=simulation_root_path,
    )


def _modern_credential_group_from_auth(
    auth: Dict[str, Any],
    host: HostSpec,
    ssh_auth_ref: str,
) -> CredentialGroup:
    ssh_auth_refs = _modern_auth_ref_names(auth)
    if not ssh_auth_refs:
        raise RemoteError("invalid auth config: missing ssh_auth.refs map")

    ssh_auth = auth.get("ssh_auth")
    assert isinstance(ssh_auth, dict)
    ssh_refs = ssh_auth.get("refs")
    assert isinstance(ssh_refs, dict)
    credential_config = ssh_refs.get(ssh_auth_ref)
    if not isinstance(credential_config, dict):
        raise RemoteError(f"invalid auth config: unknown ssh auth ref '{ssh_auth_ref}'")

    return _credential_group_from_ref(
        credential_config,
        ssh_auth_ref,
        host.login_user,
    )
def _context_from_inventory_record(
    paths: RepoPaths,
    record_kind: str,
    record_name: str,
    host_name: str,
    host_config: Dict[str, Any],
    runtime: Dict[str, Any],
) -> TargetContext:
    explicit_ssh_auth_ref = _host_ssh_auth_ref(host_config)
    ssh_auth_ref = explicit_ssh_auth_ref
    if not ssh_auth_ref:
        raise RemoteError(
            f"invalid {record_kind} config: {record_kind} '{record_name}' missing ssh_auth_ref"
        )

    host = HostSpec(
        name=host_name,
        host=_require_non_empty_string(
            host_config.get("host"),
            f"invalid {record_kind} config: host '{host_name}' missing host",
        ),
        port=_require_int_in_range(
            host_config.get("port"),
            f"invalid {record_kind} config: host '{host_name}' has invalid port",
        ),
        login_user=_require_non_empty_string(
            host_config.get("login_user"),
            f"invalid {record_kind} config: host '{host_name}' missing login_user",
        ),
        auth_group=_require_non_empty_string(
            ssh_auth_ref,
            f"invalid {record_kind} config: host '{host_name}' missing ssh_auth_ref",
        ),
        ssh_auth_ref=ssh_auth_ref,
    )

    image_ref = _require_non_empty_string(
        runtime.get("image_ref"),
        f"invalid {record_kind} config: {record_kind} '{record_name}' runtime.image_ref must be a non-empty string",
    )
    container_name = _require_non_empty_string(
        runtime.get("container_name"),
        f"invalid {record_kind} config: {record_kind} '{record_name}' runtime.container_name must be a non-empty string",
    )
    ssh_port = _require_int_in_range(
        runtime.get("ssh_port"),
        f"invalid {record_kind} config: {record_kind} '{record_name}' runtime.ssh_port must be in range 1..65535",
    )
    bootstrap_mode = _require_non_empty_string(
        runtime.get("bootstrap_mode"),
        f"invalid {record_kind} config: {record_kind} '{record_name}' runtime.bootstrap_mode must be a non-empty string",
    )

    workspace_root = runtime.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        workspace_root = DEFAULT_WORKSPACE_ROOT

    host_workspace_path = runtime.get("host_workspace_path")
    if not isinstance(host_workspace_path, str) or not host_workspace_path.strip():
        host_workspace_path = f"{DEFAULT_HOST_WORKSPACE_BASE}/{record_name}/workspace"

    docker_run_args = runtime.get("docker_run_args", [])
    if isinstance(docker_run_args, str):
        docker_args_list = [docker_run_args]
    elif isinstance(docker_run_args, list) and all(
        isinstance(item, str) and item.strip() for item in docker_run_args
    ):
        docker_args_list = [item.strip() for item in docker_run_args]
    elif docker_run_args in (None, []):
        docker_args_list = []
    else:
        raise RemoteError(
            f"invalid {record_kind} config: {record_kind} '{record_name}' runtime.docker_run_args must be a string list"
        )

    auth = _load_auth_config(paths)
    credential = _modern_credential_group_from_auth(auth, host, ssh_auth_ref)

    return TargetContext(
        name=record_name,
        host=host,
        credential=credential,
        runtime=RuntimeSpec(
            image_ref=image_ref,
            container_name=container_name,
            ssh_port=ssh_port,
            workspace_root=workspace_root.strip(),
            bootstrap_mode=bootstrap_mode,
            host_workspace_path=host_workspace_path.strip(),
            docker_run_args=docker_args_list,
        ),
    )


def _verification_runtime_payload(ctx: TargetContext, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "image_ref": ctx.runtime.image_ref,
        "container_name": ctx.runtime.container_name,
        "ssh_port": ctx.runtime.ssh_port,
        "workspace_root": ctx.runtime.workspace_root,
        "bootstrap_mode": ctx.runtime.bootstrap_mode,
        "host_name": ctx.host.name,
        "host": ctx.host.host,
        "host_port": ctx.host.port,
        "login_user": ctx.host.login_user,
        "host_workspace_path": ctx.runtime.host_workspace_path,
    }
    payload.update(extra)
    return payload
def resolve_server_context(paths: RepoPaths, server_name: str) -> TargetContext:
    config = _load_servers_config(paths)
    servers = config.get("servers")
    if not isinstance(servers, dict):
        raise RemoteError("invalid server config: missing 'servers' map")

    server = servers.get(server_name)
    if not isinstance(server, dict):
        raise RemoteError(f"unknown server: {server_name}")

    runtime = server.get("runtime")
    if not isinstance(runtime, dict):
        raise RemoteError(
            f"invalid server config: server '{server_name}' missing runtime map"
        )

    return _context_from_inventory_record(
        paths,
        "server",
        server_name,
        server_name,
        server,
        runtime,
    )


def list_managed_server_names(paths: RepoPaths) -> List[str]:
    if not paths.local_servers_file.exists():
        return []
    config = _load_servers_config(paths)
    servers = config.get("servers")
    if not isinstance(servers, dict):
        raise RemoteError("invalid server config: missing 'servers' map")
    return sorted(
        name.strip()
        for name in servers
        if isinstance(name, str) and name.strip()
    )
def persist_server_verification(
    paths: RepoPaths,
    server_name: str,
    verification: VerificationResult,
) -> None:
    config = _load_servers_config(paths)
    servers = config.get("servers")
    if not isinstance(servers, dict):
        raise RemoteError("invalid server config: missing 'servers' map")

    server = servers.get(server_name)
    if not isinstance(server, dict):
        raise RemoteError(f"unknown server: {server_name}")

    server["status"] = verification.status
    server.pop("verification", None)
    config["version"] = 1
    config["servers"] = servers
    paths.local_servers_file.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    state = read_capability_state(paths)
    servers_state = state.setdefault("servers", {})
    if not isinstance(servers_state, dict):
        raise RemoteError("invalid runtime state: .workspace.local/state.json")
    server_state = servers_state.setdefault(server_name, {})
    if not isinstance(server_state, dict):
        server_state = {}
        servers_state[server_name] = server_state
    server_state["container_access"] = {
        "status": verification.status,
        "mode": "ssh-key",
        "detail": verification.summary,
        "observed_at": _observed_at(),
        "evidence_source": "machine-management",
    }
    if verification.status == "ready":
        server_state["host_access"] = {
            "status": "ready",
            "mode": "ssh-key",
            "detail": "host ssh ready",
            "observed_at": _observed_at(),
            "evidence_source": "machine-management",
        }
        state["current_server"] = server_name
        state.pop("current_session", None)
    write_capability_state(paths, state)


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _peer_mesh_leaf(status: str, detail: str) -> Dict[str, Any]:
    return {
        "status": status,
        "detail": detail,
        "observed_at": _observed_at(),
        "evidence_source": "machine-management",
    }


def list_ready_servers(paths: RepoPaths) -> List[str]:
    state = read_capability_state(paths)
    servers = state.get("servers")
    if not isinstance(servers, dict):
        return []
    ready = []
    for server_name, payload in servers.items():
        if not isinstance(server_name, str) or not server_name.strip():
            continue
        container_access = payload.get("container_access") if isinstance(payload, dict) else None
        if isinstance(container_access, dict) and container_access.get("status") == "ready":
            ready.append(server_name.strip())
    return sorted(ready)


def _probe_peer_connectivity(paths: RepoPaths, server_name: str, peer_name: str) -> bool:
    ctx = resolve_server_context(paths, server_name)
    peer_ctx = resolve_server_context(paths, peer_name)
    script = (
        "set -e\n"
        f"ping -c 1 -W 1 {shlex.quote(peer_ctx.host.host)} >/dev/null 2>&1"
    )
    result = run_runtime_command(ctx, "container-ssh", script)
    return result.returncode == 0


def _read_container_public_key(paths: RepoPaths, server_name: str) -> str:
    ctx = resolve_server_context(paths, server_name)
    result = run_runtime_command(
        ctx,
        "container-ssh",
        "set -euo pipefail\ncat /root/.ssh/id_ed25519.pub",
    )
    if result.returncode != 0:
        raise RemoteError((result.stderr or result.stdout).strip())
    public_key = result.stdout.strip()
    if not public_key:
        raise RemoteError(f"missing container public key for server '{server_name}'")
    return public_key


def _install_peer_authorized_key(paths: RepoPaths, server_name: str, peer_name: str) -> None:
    ctx = resolve_server_context(paths, server_name)
    peer_key = _read_container_public_key(paths, peer_name)
    script = "\n".join(
        [
            "set -euo pipefail",
            "mkdir -p /root/.ssh",
            "chmod 700 /root/.ssh",
            "touch /root/.ssh/authorized_keys",
            "chmod 600 /root/.ssh/authorized_keys",
            f"grep -qxF {shlex.quote(peer_key)} /root/.ssh/authorized_keys || "
            f"printf '%s\\n' {shlex.quote(peer_key)} >> /root/.ssh/authorized_keys",
        ]
    )
    result = run_runtime_command(ctx, "container-ssh", script)
    if result.returncode != 0:
        raise RemoteError((result.stderr or result.stdout).strip())


def _warm_peer_known_hosts(paths: RepoPaths, server_name: str, peer_name: str) -> None:
    ctx = resolve_server_context(paths, server_name)
    peer_ctx = resolve_server_context(paths, peer_name)
    script = "\n".join(
        [
            "set -euo pipefail",
            "mkdir -p /root/.ssh",
            "touch /root/.ssh/known_hosts",
            "chmod 600 /root/.ssh/known_hosts",
            f"ssh-keyscan -p {peer_ctx.runtime.ssh_port} {shlex.quote(peer_ctx.host.host)} "
            "2>/dev/null >> /root/.ssh/known_hosts",
        ]
    )
    result = run_runtime_command(ctx, "container-ssh", script)
    if result.returncode != 0:
        raise RemoteError((result.stderr or result.stdout).strip())


def reconcile_peer_mesh(paths: RepoPaths, server_name: str) -> None:
    peers = [peer_name for peer_name in list_ready_servers(paths) if peer_name != server_name]
    if not peers:
        write_capability_leaf(
            paths,
            ("servers", server_name, "peer_mesh"),
            _peer_mesh_leaf("ready", "no other ready peers"),
        )
        return

    any_success = False
    any_degraded = False
    for peer_name in peers:
        try:
            if not _probe_peer_connectivity(paths, server_name, peer_name):
                any_degraded = True
                continue
            _install_peer_authorized_key(paths, server_name, peer_name)
            _install_peer_authorized_key(paths, peer_name, server_name)
            _warm_peer_known_hosts(paths, server_name, peer_name)
            _warm_peer_known_hosts(paths, peer_name, server_name)
            any_success = True
        except (RemoteError, RuntimeError):
            any_degraded = True

    if any_degraded:
        write_capability_leaf(
            paths,
            ("servers", server_name, "peer_mesh"),
            _peer_mesh_leaf("degraded_optional", "one or more peers were unreachable"),
        )
        return

    if any_success:
        write_capability_leaf(
            paths,
            ("servers", server_name, "peer_mesh"),
            _peer_mesh_leaf("ready", "peer mesh reconciled"),
        )


def _simulation_runtime_status(ctx: TargetContext) -> str:
    runtime_root = _simulation_runtime_root(ctx)
    if not runtime_root.exists():
        return "absent"
    return "present"


def _host_runtime_status(ctx: TargetContext) -> str:
    probe = subprocess.run(
        _ssh_base_command(ctx) + ["true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        raise RemoteError(
            f"unable to probe host {ctx.host.host}: {(probe.stderr or probe.stdout or 'SSH probe failed').strip()}"
        )

    script = "\n".join(
        [
            "set -e",
            f"container_names=$(docker container ls -a --format '{{{{.Names}}}}')",
            f"if printf '%s\\n' \"$container_names\" | grep -Fqx {shlex.quote(ctx.runtime.container_name)}; then",
            "  echo present",
            f"elif [ -e {shlex.quote(ctx.runtime.host_workspace_path)} ]; then",
            "  echo present",
            "else",
            "  echo absent",
            "fi",
        ]
    )
    result = _run_host_command(ctx, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to inspect remote runtime for server '{ctx.name}': {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip() or "present"


def cleanup_runtime(ctx: TargetContext) -> CleanupResult:
    if ctx.credential.mode == "local-simulation":
        if _simulation_runtime_status(ctx) == "absent":
            return CleanupResult(
                server_name=ctx.name,
                status="already_absent",
                detail="simulation runtime already absent",
            )
        try:
            destroy_runtime(ctx)
        except RemoteError as exc:
            return CleanupResult(
                server_name=ctx.name,
                status="cleanup_failed",
                detail=str(exc),
            )
        return CleanupResult(
            server_name=ctx.name,
            status="removed",
            detail="simulation runtime removed",
        )

    try:
        status = _host_runtime_status(ctx)
    except RemoteError as exc:
        message = str(exc)
        if "probe" in message or "ssh" in message or "authenticate" in message:
            return CleanupResult(
                server_name=ctx.name,
                status="unreachable",
                detail=message,
            )
        return CleanupResult(
            server_name=ctx.name,
            status="cleanup_failed",
            detail=message,
        )

    if status == "absent":
        return CleanupResult(
            server_name=ctx.name,
            status="already_absent",
            detail="remote runtime already absent",
        )

    try:
        destroy_runtime(ctx)
    except RemoteError as exc:
        message = str(exc)
        if "probe" in message or "ssh" in message or "authenticate" in message:
            return CleanupResult(
                server_name=ctx.name,
                status="unreachable",
                detail=message,
            )
        return CleanupResult(
            server_name=ctx.name,
            status="cleanup_failed",
            detail=message,
        )

    return CleanupResult(
        server_name=ctx.name,
        status="removed",
        detail="remote runtime removed",
    )


def cleanup_managed_servers(paths: RepoPaths) -> List[CleanupResult]:
    results: List[CleanupResult] = []
    for server_name in list_managed_server_names(paths):
        try:
            context = resolve_server_context(paths, server_name)
        except RemoteError as exc:
            results.append(
                CleanupResult(
                    server_name=server_name,
                    status="cleanup_failed",
                    detail=str(exc),
                )
            )
            continue
        results.append(cleanup_runtime(context))
    return results


def _runtime_state_file(runtime_root: Path) -> Path:
    return runtime_root / ".vaws" / "runtime.json"


def _ensure_symlink(link_path: Path, target_path: Path) -> None:
    if link_path.is_symlink():
        if link_path.resolve() == target_path.resolve():
            return
        link_path.unlink()
    elif link_path.exists():
        if link_path.is_dir():
            for child in link_path.iterdir():
                if child.is_dir():
                    for nested in child.iterdir():
                        if nested.is_dir():
                            pass
            if link_path.is_dir():
                import shutil

                shutil.rmtree(link_path)
        else:
            link_path.unlink()
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(target_path)


def _simulation_runtime_root(ctx: TargetContext) -> Path:
    assert ctx.credential.simulation_root is not None
    relative_root = ctx.runtime.workspace_root.lstrip("/")
    return ctx.credential.simulation_root / ctx.host.name / relative_root


def _load_runtime_state(runtime_root: Path) -> Dict[str, Any]:
    runtime_state_path = _runtime_state_file(runtime_root)
    if not runtime_state_path.is_file():
        return {}
    return json.loads(runtime_state_path.read_text(encoding="utf-8"))


def _write_runtime_state(runtime_root: Path, runtime_state: Dict[str, Any]) -> None:
    runtime_state_path = _runtime_state_file(runtime_root)
    runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_state_path.write_text(
        json.dumps(runtime_state, indent=2) + "\n",
        encoding="utf-8",
    )


def _ensure_simulation_runtime(paths: RepoPaths, ctx: TargetContext) -> Dict[str, Any]:
    runtime_root = _simulation_runtime_root(ctx)
    reused = runtime_root.exists()
    (runtime_root / ".vaws" / "targets").mkdir(parents=True, exist_ok=True)

    control_repo_path = runtime_root / "workspace"
    _ensure_symlink(control_repo_path, paths.root)

    runtime_state = _load_runtime_state(runtime_root)
    runtime_state.update(
        {
            "target": ctx.name,
            "workspace_root": ctx.runtime.workspace_root,
            "control_repo_path": str(control_repo_path),
            "container": {
                "name": ctx.runtime.container_name,
                "created": True,
                "reused": reused,
            },
            "endpoint": {
                "transport": "simulation",
                "host": ctx.host.host,
                "port": ctx.runtime.ssh_port,
            },
        }
    )
    _write_runtime_state(runtime_root, runtime_state)
    return {
        "image_ref": ctx.runtime.image_ref,
        "container_name": ctx.runtime.container_name,
        "ssh_port": ctx.runtime.ssh_port,
        "workspace_root": ctx.runtime.workspace_root,
        "bootstrap_mode": ctx.runtime.bootstrap_mode,
        "transport": "simulation",
        "container_endpoint": f"simulation://{ctx.host.name}{ctx.runtime.workspace_root}",
        "host_name": ctx.host.name,
        "host": ctx.host.host,
        "host_port": ctx.host.port,
        "login_user": ctx.host.login_user,
        "host_workspace_path": str(control_repo_path),
    }


def _find_public_key_path() -> Path:
    ssh_dir = Path.home() / ".ssh"
    for candidate in ("id_ed25519.pub", "id_rsa.pub"):
        path = ssh_dir / candidate
        if path.is_file():
            return path
    public_keys = sorted(ssh_dir.glob("*.pub"))
    if public_keys:
        return public_keys[0]
    raise RemoteError("missing local ssh public key: expected ~/.ssh/*.pub")


def _ssh_base_command(
    ctx: TargetContext,
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


def _verify_host_ssh_key_login(ctx: TargetContext) -> None:
    probe = subprocess.run(
        _ssh_base_command(ctx) + ["true"],
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


def _install_local_public_key_on_host(ctx: TargetContext) -> None:
    password = ctx.credential.resolved_password
    if not password:
        raise RemoteError(
            f"cannot authenticate to host {ctx.host.host}: SSH key failed and no password is configured"
        )

    try:
        import pexpect
    except ImportError as exc:
        raise RemoteError(
            "password-based host bootstrap requires pexpect to be installed locally"
        ) from exc

    public_key_text = _find_public_key_path().read_text(encoding="utf-8").strip()
    remote_script = (
        "umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
        f"grep -qxF {shlex.quote(public_key_text)} ~/.ssh/authorized_keys || "
        f"printf '%s\\n' {shlex.quote(public_key_text)} >> ~/.ssh/authorized_keys"
    )
    command = shlex.join(
        _ssh_base_command(ctx, batch_mode=False) + [f"bash -lc {shlex.quote(remote_script)}"]
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


def _promote_host_auth_ref_to_ssh_key(paths: RepoPaths, ctx: TargetContext) -> None:
    if ctx.credential.mode != "password" or not ctx.host.ssh_auth_ref:
        return

    auth = _load_auth_config(paths)
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


def _ensure_host_ssh_access(
    ctx: TargetContext,
    *,
    allow_password_bootstrap: bool = False,
) -> bool:
    probe = subprocess.run(
        _ssh_base_command(ctx) + ["true"],
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

    _install_local_public_key_on_host(ctx)
    _verify_host_ssh_key_login(ctx)
    return True


def _run_host_command(ctx: TargetContext, script: str) -> subprocess.CompletedProcess[str]:
    _ensure_host_ssh_access(ctx, allow_password_bootstrap=False)
    return subprocess.run(
        _ssh_base_command(ctx) + [f"bash -lc {shlex.quote(script)}"],
        text=True,
        capture_output=True,
        check=False,
    )


def _ensure_host_path(ctx: TargetContext) -> None:
    script = f"mkdir -p {shlex.quote(ctx.runtime.host_workspace_path)}"
    result = _run_host_command(ctx, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to prepare host workspace path: {(result.stderr or result.stdout).strip()}"
        )


def _stream_repo_to_host(paths: RepoPaths, ctx: TargetContext) -> None:
    _ensure_host_path(ctx)
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
        cwd=str(paths.root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ssh_process = subprocess.Popen(
        _ssh_base_command(ctx) + [
            f"tar -xf - -C {shlex.quote(ctx.runtime.host_workspace_path)}"
        ],
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


def _docker_run_command(ctx: TargetContext) -> str:
    mount_spec = (
        f"{ctx.runtime.host_workspace_path}:{ctx.runtime.workspace_root}/workspace"
    )
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
    base_args.extend(
        [
            ctx.runtime.image_ref,
            "bash",
            "-lc",
            "while true; do sleep 3600; done",
        ]
    )
    return shlex.join(base_args)


def _inspect_container_contract(ctx: TargetContext) -> Dict[str, Any]:
    inspect_result = _run_host_command(
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
        raise RemoteError(
            f"invalid container inspection output for '{ctx.runtime.container_name}'"
        ) from exc

    mounts = sorted(
        f"{mount.get('Source', '')}:{mount.get('Destination', '')}"
        for mount in payload.get("Mounts", [])
        if isinstance(mount, dict)
    )
    sshd_config = ""
    state = payload.get("State")
    if isinstance(state, dict) and state.get("Running") is True:
        sshd_probe = _run_docker_exec(
            ctx,
            "cat /etc/ssh/sshd_config 2>/dev/null || true",
        )
        if sshd_probe.returncode == 0:
            sshd_config = sshd_probe.stdout or ""

    return {
        "image": payload.get("Config", {}).get("Image"),
        "network_mode": payload.get("HostConfig", {}).get("NetworkMode"),
        "mounts": mounts,
        "sshd_config": sshd_config,
    }


def _container_requires_rebuild(ctx: TargetContext) -> bool:
    contract = _inspect_container_contract(ctx)
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


def _remove_host_container(ctx: TargetContext) -> None:
    remove = _run_host_command(
        ctx,
        f"docker rm -f {shlex.quote(ctx.runtime.container_name)} >/dev/null",
    )
    if remove.returncode != 0:
        raise RemoteError(
            f"failed to remove container '{ctx.runtime.container_name}': "
            f"{(remove.stderr or remove.stdout).strip()}"
        )


def _ensure_host_container(ctx: TargetContext) -> bool:
    inspect = _run_host_command(
        ctx,
        f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(ctx.runtime.container_name)} 2>/dev/null || true",
    )
    running_state = (inspect.stdout or "").strip()
    if running_state == "true":
        if _container_requires_rebuild(ctx):
            _remove_host_container(ctx)
        else:
            return True
    elif running_state == "false":
        start = _run_host_command(
            ctx,
            f"docker start {shlex.quote(ctx.runtime.container_name)} >/dev/null",
        )
        if start.returncode != 0:
            raise RemoteError(
                f"failed to start existing container '{ctx.runtime.container_name}': "
                f"{(start.stderr or start.stdout).strip()}"
            )
        if _container_requires_rebuild(ctx):
            _remove_host_container(ctx)
        else:
            return True

    create = _run_host_command(ctx, _docker_run_command(ctx))
    if create.returncode != 0:
        raise RemoteError(
            f"failed to create container '{ctx.runtime.container_name}': "
            f"{(create.stderr or create.stdout).strip()}"
        )
    return False


def _run_docker_exec(ctx: TargetContext, script: str) -> subprocess.CompletedProcess[str]:
    docker_script = (
        f"docker exec -i {shlex.quote(ctx.runtime.container_name)} "
        f"bash -lc {shlex.quote(script)}"
    )
    return _run_host_command(ctx, docker_script)


def _probe_container_ssh(ctx: TargetContext) -> bool:
    status, _detail = _probe_container_ssh_transport(ctx)
    return status == "ready"


def _probe_container_ssh_transport(ctx: TargetContext) -> tuple[str, str]:
    probe = subprocess.run(
        _ssh_base_command(ctx, port=ctx.runtime.ssh_port, username="root") + ["true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return "ready", "container SSH probe succeeded"
    return "needs_repair", (
        probe.stderr or probe.stdout or "container SSH probe failed"
    ).strip()


def _probe_docker_exec_transport(ctx: TargetContext) -> tuple[str, str]:
    try:
        probe = _run_docker_exec(ctx, "true")
    except RemoteError as exc:
        return "needs_repair", str(exc)
    if probe.returncode == 0:
        return "ready", "docker exec probe succeeded"
    return "needs_repair", (
        probe.stderr or probe.stdout or "docker exec probe failed"
    ).strip()


def _bootstrap_container_runtime(ctx: TargetContext) -> str:
    public_key = _find_public_key_path().read_text(encoding="utf-8").strip()
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
            f"pkill -f 'sshd -p {ctx.runtime.ssh_port}' >/dev/null 2>&1 || true",
            f"(sshd -t && sshd -p {ctx.runtime.ssh_port}) >/dev/null 2>&1 || "
            f"(/usr/sbin/sshd -t && /usr/sbin/sshd -p {ctx.runtime.ssh_port}) >/dev/null 2>&1",
        ]
    )
    result = _run_docker_exec(ctx, bootstrap_script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to bootstrap runtime inside container: {(result.stderr or result.stdout).strip()}"
        )
    if _probe_container_ssh(ctx):
        return "container-ssh"
    return "docker-exec"


def _run_container_command(
    ctx: TargetContext,
    transport: str,
    script: str,
) -> subprocess.CompletedProcess[str]:
    if transport == "container-ssh":
        return subprocess.run(
            _ssh_base_command(ctx, port=ctx.runtime.ssh_port, username="root")
            + [f"bash -lc {shlex.quote(script)}"],
            text=True,
            capture_output=True,
            check=False,
        )
    return _run_docker_exec(ctx, script)


def _write_host_runtime_state(ctx: TargetContext, transport: str) -> None:
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
    result = _run_container_command(ctx, transport, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to record runtime state in container: {(result.stderr or result.stdout).strip()}"
        )


def _ensure_host_runtime(paths: RepoPaths, ctx: TargetContext) -> Dict[str, Any]:
    _ensure_host_path(ctx)
    reused = _ensure_host_container(ctx)
    transport = _bootstrap_container_runtime(ctx)
    _write_host_runtime_state(ctx, transport)
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
        "reused": reused,
    }


def ensure_runtime(paths: RepoPaths, ctx: TargetContext) -> Dict[str, Any]:
    if ctx.credential.mode == "local-simulation":
        return _ensure_simulation_runtime(paths, ctx)
    _ensure_host_ssh_access(ctx, allow_password_bootstrap=True)
    _promote_host_auth_ref_to_ssh_key(paths, ctx)
    return _ensure_host_runtime(paths, ctx)


def _verify_simulation_runtime(ctx: TargetContext) -> VerificationResult:
    runtime_root = _simulation_runtime_root(ctx)
    runtime_state = _load_runtime_state(runtime_root)
    if not runtime_state:
        return VerificationResult.needs_repair(
            summary=f"missing runtime state for server {ctx.name}",
            runtime=_verification_runtime_payload(
                ctx,
                transport="simulation",
                container_endpoint=f"simulation://{ctx.host.name}{ctx.runtime.workspace_root}",
            ),
            checks=[
                VerificationCheck(
                    name="runtime_state",
                    status="needs_repair",
                    detail="missing runtime state",
                )
            ],
        )

    endpoint = runtime_state.get("endpoint")
    if not isinstance(endpoint, dict):
        return VerificationResult.needs_repair(
            summary=f"invalid runtime state for server {ctx.name}",
            runtime=_verification_runtime_payload(
                ctx,
                transport="simulation",
                container_endpoint=f"simulation://{ctx.host.name}{ctx.runtime.workspace_root}",
            ),
            checks=[
                VerificationCheck(
                    name="runtime_state",
                    status="needs_repair",
                    detail="invalid runtime state",
                )
            ],
        )

    return VerificationResult.ready(
        summary=f"runtime ready for server {ctx.name}",
        runtime=_verification_runtime_payload(
            ctx,
            transport=endpoint.get("transport", "simulation"),
            container_endpoint=f"simulation://{ctx.host.name}{ctx.runtime.workspace_root}",
            host_workspace_path=str(runtime_root / "workspace"),
        ),
        checks=[
            VerificationCheck(
                name="runtime_state",
                status="ready",
                detail="runtime state available",
            )
        ],
    )


def _verify_host_runtime(ctx: TargetContext) -> VerificationResult:
    container_ssh_status, container_ssh_detail = _probe_container_ssh_transport(ctx)
    container_ssh_check = VerificationCheck(
        name="container_ssh",
        status=container_ssh_status,
        detail=container_ssh_detail,
    )
    if container_ssh_status == "ready":
        return VerificationResult.ready(
            summary=f"runtime ready for host {ctx.host.host} via container ssh",
            runtime=_verification_runtime_payload(
                ctx,
                transport="container-ssh",
                container_endpoint=f"ssh://root@{ctx.host.host}:{ctx.runtime.ssh_port}",
            ),
            checks=[container_ssh_check],
        )

    docker_exec_status, docker_exec_detail = _probe_docker_exec_transport(ctx)
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
        runtime=_verification_runtime_payload(
            ctx,
            transport="container-ssh",
            container_endpoint=f"ssh://root@{ctx.host.host}:{ctx.runtime.ssh_port}",
        ),
        checks=[container_ssh_check, docker_exec_check],
    )


def verify_runtime(paths: RepoPaths, ctx: TargetContext) -> VerificationResult:
    if ctx.credential.mode == "local-simulation":
        return _verify_simulation_runtime(ctx)
    return _verify_host_runtime(ctx)


def run_runtime_command(
    ctx: TargetContext,
    transport: str,
    script: str,
) -> subprocess.CompletedProcess[str]:
    return _run_container_command(ctx, transport, script)


def run_detached_runtime_command(
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
    return run_runtime_command(ctx, transport, wrapped)


def _destroy_simulation_runtime(ctx: TargetContext) -> None:
    runtime_root = _simulation_runtime_root(ctx)
    if not runtime_root.exists():
        return
    try:
        parent_dir = runtime_root.parent
        if not os.access(parent_dir, os.W_OK | os.X_OK):
            raise RemoteError(
                f"failed to clean simulation runtime at {runtime_root}: permission denied"
            )
        if runtime_root.is_symlink():
            runtime_root.unlink()
        else:
            shutil.rmtree(runtime_root)
    except OSError as exc:
        raise RemoteError(
            f"failed to clean simulation runtime at {runtime_root}: {exc}"
        ) from exc


def _destroy_host_runtime(ctx: TargetContext) -> None:
    script = "\n".join(
        [
            "set -e",
            f"container_names=$(docker container ls -a --format '{{{{.Names}}}}') || exit 1",
            f"if printf '%s\\n' \"$container_names\" | grep -Fqx {shlex.quote(ctx.runtime.container_name)}; then",
            f"  docker rm -f {shlex.quote(ctx.runtime.container_name)} >/dev/null",
            "fi",
            f"rm -rf {shlex.quote(ctx.runtime.host_workspace_path)}",
        ]
    )
    result = _run_host_command(ctx, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to clean remote runtime for target '{ctx.name}': "
            f"{(result.stderr or result.stdout).strip()}"
        )


def destroy_runtime(ctx: TargetContext) -> None:
    if ctx.credential.mode == "local-simulation":
        _destroy_simulation_runtime(ctx)
        return
    _destroy_host_runtime(ctx)
