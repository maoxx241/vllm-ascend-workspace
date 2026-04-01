from __future__ import annotations

import json
import os
import shlex
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import RepoPaths
from .runtime import read_state, write_state

DEFAULT_HOST_WORKSPACE_BASE = "/root/.vaws/targets"
DEFAULT_WORKSPACE_ROOT = "/vllm-workspace"


class RemoteError(RuntimeError):
    """Remote bootstrap or runtime materialization failure."""


def can_fallback_to_legacy_target(exc: "RemoteError") -> bool:
    message = str(exc)
    return (
        message.startswith("unknown server:")
        or message.startswith("invalid server config: .workspace.local/servers.yaml")
        or message.startswith("invalid server config: missing 'servers' map")
    )


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


def _load_targets_config(paths: RepoPaths) -> Dict[str, Any]:
    return _read_yaml_mapping(
        paths.local_overlay / "targets.yaml",
        "invalid target config: .workspace.local/targets.yaml",
    )


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


def _host_auth_ref(host_config: Dict[str, Any]) -> Optional[str]:
    auth_ref = host_config.get("ssh_auth_ref")
    if isinstance(auth_ref, str) and auth_ref.strip():
        return auth_ref.strip()
    auth_group = host_config.get("auth_group")
    if isinstance(auth_group, str) and auth_group.strip():
        return auth_group.strip()
    return None


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


def _legacy_auth_ref_names(auth: Dict[str, Any]) -> List[str]:
    host_auth = auth.get("host_auth")
    if not isinstance(host_auth, dict):
        return []

    credential_groups = host_auth.get("credential_groups")
    if not isinstance(credential_groups, dict):
        return []

    return sorted(
        ref_name.strip()
        for ref_name in credential_groups
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


def _legacy_credential_group_from_auth(auth: Dict[str, Any], host: HostSpec) -> CredentialGroup:
    host_auth = auth.get("host_auth")
    if not isinstance(host_auth, dict):
        raise RemoteError("invalid auth config: missing ssh_auth.refs map")

    mode = host_auth.get("mode", "ssh-key")
    if not isinstance(mode, str) or not mode.strip():
        raise RemoteError("invalid auth config: host_auth.mode must be a string")

    credential_groups = host_auth.get("credential_groups")
    if not isinstance(credential_groups, dict):
        raise RemoteError("invalid auth config: missing host_auth.credential_groups map")

    credential_config = credential_groups.get(host.auth_group)
    if not isinstance(credential_config, dict):
        raise RemoteError(f"invalid auth config: unknown auth group '{host.auth_group}'")

    username = credential_config.get("username", host.login_user)
    if not isinstance(username, str) or not username.strip():
        raise RemoteError(
            f"invalid auth config: auth group '{host.auth_group}' missing username"
        )

    simulation_root = credential_config.get("simulation_root")
    if mode == "local-simulation":
        if not isinstance(simulation_root, str) or not simulation_root.strip():
            raise RemoteError(
                f"invalid auth config: auth group '{host.auth_group}' missing simulation_root"
            )
        simulation_root_path = Path(simulation_root)
    else:
        simulation_root_path = None

    return CredentialGroup(
        mode=mode.strip(),
        username=username.strip(),
        password=credential_config.get("password"),
        password_env=credential_config.get("password_env"),
        key_path=credential_config.get("key_path"),
        token_env=credential_config.get("token_env"),
        simulation_root=simulation_root_path,
    )


def _context_from_inventory_record(
    paths: RepoPaths,
    record_kind: str,
    record_name: str,
    host_name: str,
    host_config: Dict[str, Any],
    runtime: Dict[str, Any],
    *,
    legacy_auth: bool = False,
) -> TargetContext:
    explicit_ssh_auth_ref = _host_ssh_auth_ref(host_config)
    ssh_auth_ref = explicit_ssh_auth_ref
    if not ssh_auth_ref and legacy_auth:
        ssh_auth_ref = _host_auth_ref(host_config)
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
    if legacy_auth and explicit_ssh_auth_ref:
        credential = _modern_credential_group_from_auth(auth, host, explicit_ssh_auth_ref)
    elif legacy_auth:
        credential = _legacy_credential_group_from_auth(auth, host)
    else:
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


def _resolve_legacy_target_context(paths: RepoPaths, target_name: str) -> TargetContext:
    config = _load_targets_config(paths)
    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise RemoteError("invalid target config: missing 'targets' map")

    target = targets.get(target_name)
    if not isinstance(target, dict):
        raise RemoteError(f"unknown target: {target_name}")

    runtime = target.get("runtime")
    if not isinstance(runtime, dict):
        raise RemoteError(
            f"invalid target config: target '{target_name}' missing runtime map"
        )
    required_runtime_fields = ("image_ref", "container_name", "ssh_port", "bootstrap_mode")
    missing_runtime_fields = [name for name in required_runtime_fields if name not in runtime]
    if missing_runtime_fields:
        raise RemoteError(
            "invalid target config: "
            f"target '{target_name}' has incomplete runtime config (missing: "
            f"{', '.join(missing_runtime_fields)})"
        )

    hosts = config.get("hosts")
    if not isinstance(hosts, dict):
        raise RemoteError("invalid target config: missing 'hosts' map")

    target_hosts = target.get("hosts")
    if not isinstance(target_hosts, list) or not target_hosts:
        raise RemoteError(
            f"invalid target config: target '{target_name}' missing non-empty hosts list"
        )

    host_name = target_hosts[0]
    if not isinstance(host_name, str) or not host_name.strip():
        raise RemoteError(
            f"invalid target config: target '{target_name}' has invalid host entry"
        )

    host_config = hosts.get(host_name)
    if not isinstance(host_config, dict):
        raise RemoteError(
            f"invalid target config: target '{target_name}' references unknown host '{host_name}'"
        )

    return _context_from_inventory_record(
        paths,
        "target",
        target_name,
        host_name,
        host_config,
        runtime,
        legacy_auth=True,
    )


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


def resolve_target_context(paths: RepoPaths, target_name: str) -> TargetContext:
    return _resolve_legacy_target_context(paths, target_name)


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
    server["verification"] = verification.to_mapping()
    config["version"] = 1
    config["servers"] = servers
    paths.local_servers_file.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )

    state = read_state(paths)
    server_verifications = state.get("server_verifications")
    if server_verifications is None:
        server_verifications = {}
    elif not isinstance(server_verifications, dict):
        raise RemoteError("invalid runtime state: .workspace.local/state.json")

    server_verifications[server_name] = verification.to_mapping()
    state["server_verifications"] = server_verifications
    write_state(paths, state)


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


def _runtime_wrapper_text(workspace_root: str) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            'session_name="${1:-}"',
            'if [ -z "${session_name}" ]; then',
            '  echo "usage: enter-env <session> -- <command>" >&2',
            "  exit 2",
            "fi",
            "shift",
            'if [ "${1:-}" = "--" ]; then',
            "  shift",
            "fi",
            f'venv_path="{workspace_root}/.vaws/sessions/${{session_name}}/.venv"',
            'if [ ! -x "${venv_path}/bin/python" ]; then',
            '  python3 -m venv "${venv_path}"',
            "fi",
            'source "${venv_path}/bin/activate"',
            'exec "$@"',
            "",
        ]
    )


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


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _ensure_local_worktree(
    source_repo: Path,
    destination: Path,
    branch: str,
    base_ref: str,
) -> None:
    if _is_git_repo(destination):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not _is_git_repo(source_repo):
        destination.mkdir(parents=True, exist_ok=True)
        return

    result = subprocess.run(
        [
            "git",
            "-C",
            str(source_repo),
            "worktree",
            "add",
            "--force",
            "-B",
            branch,
            str(destination),
            base_ref,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RemoteError(
            f"failed to materialize worktree {destination.name}: "
            f"{(result.stderr or result.stdout).strip()}"
        )


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
    (runtime_root / ".vaws" / "sessions").mkdir(parents=True, exist_ok=True)
    wrapper_path = runtime_root / ".vaws" / "bin" / "enter-env"
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(
        _runtime_wrapper_text(ctx.runtime.workspace_root),
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755)

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


def _ensure_simulation_session(paths: RepoPaths, ctx: TargetContext, manifest: Dict[str, Any]) -> None:
    runtime_root = _simulation_runtime_root(ctx)
    session_name = manifest["name"]
    session_root = runtime_root / ".vaws" / "sessions" / session_name
    session_root.mkdir(parents=True, exist_ok=True)
    (session_root / ".venv").mkdir(parents=True, exist_ok=True)
    (session_root / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    control_repo = runtime_root / "workspace"
    _ensure_local_worktree(
        control_repo / "vllm",
        session_root / "vllm",
        manifest["vllm_ref"]["branch"],
        manifest["vllm_ref"]["base_ref"],
    )
    _ensure_local_worktree(
        control_repo / "vllm-ascend",
        session_root / "vllm-ascend",
        manifest["vllm_ascend_ref"]["branch"],
        manifest["vllm_ascend_ref"]["base_ref"],
    )


def _switch_simulation_session(ctx: TargetContext, session_name: str) -> None:
    runtime_root = _simulation_runtime_root(ctx)
    session_root = runtime_root / ".vaws" / "sessions" / session_name
    if not session_root.is_dir():
        raise RemoteError(f"unknown session: {session_name}")

    _ensure_symlink(runtime_root / ".vaws" / "current", session_root)
    _ensure_symlink(runtime_root / "vllm", session_root / "vllm")
    _ensure_symlink(runtime_root / "vllm-ascend", session_root / "vllm-ascend")
    runtime_state = _load_runtime_state(runtime_root)
    runtime_state["current_session"] = session_name
    _write_runtime_state(runtime_root, runtime_state)


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


def _ensure_host_ssh_access(ctx: TargetContext) -> None:
    probe = subprocess.run(
        _ssh_base_command(ctx) + ["true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return

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

    verify = subprocess.run(
        _ssh_base_command(ctx) + ["true"],
        text=True,
        capture_output=True,
        check=False,
    )
    if verify.returncode != 0:
        raise RemoteError(f"unable to verify SSH key access to host {ctx.host.host}")


def _run_host_command(ctx: TargetContext, script: str) -> subprocess.CompletedProcess[str]:
    _ensure_host_ssh_access(ctx)
    return subprocess.run(
        _ssh_base_command(ctx) + [f"bash -lc {shlex.quote(script)}"],
        text=True,
        capture_output=True,
        check=False,
    )


def _ensure_host_path(ctx: TargetContext) -> None:
    script = (
        f"mkdir -p {shlex.quote(ctx.runtime.host_workspace_path)} && "
        f"find {shlex.quote(ctx.runtime.host_workspace_path)} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +"
    )
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


def _ensure_host_container(ctx: TargetContext) -> bool:
    inspect = _run_host_command(
        ctx,
        f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(ctx.runtime.container_name)} 2>/dev/null || true",
    )
    running_state = (inspect.stdout or "").strip()
    if running_state == "true":
        return True
    if running_state == "false":
        start = _run_host_command(
            ctx,
            f"docker start {shlex.quote(ctx.runtime.container_name)} >/dev/null",
        )
        if start.returncode != 0:
            raise RemoteError(
                f"failed to start existing container '{ctx.runtime.container_name}': "
                f"{(start.stderr or start.stdout).strip()}"
            )
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
    wrapper_text = _runtime_wrapper_text(ctx.runtime.workspace_root)
    bootstrap_script = "\n".join(
        [
            "set -e",
            f"mkdir -p {shlex.quote(ctx.runtime.workspace_root)}",
            f"mkdir -p {shlex.quote(ctx.runtime.workspace_root + '/.vaws/targets')}",
            f"mkdir -p {shlex.quote(ctx.runtime.workspace_root + '/.vaws/sessions')}",
            f"mkdir -p {shlex.quote(ctx.runtime.workspace_root + '/.vaws/bin')}",
            f"cat > {shlex.quote(ctx.runtime.workspace_root + '/.vaws/bin/enter-env')} <<'EOF'",
            wrapper_text.rstrip(),
            "EOF",
            f"chmod +x {shlex.quote(ctx.runtime.workspace_root + '/.vaws/bin/enter-env')}",
            f"git config --global --add safe.directory {shlex.quote(ctx.runtime.workspace_root + '/workspace')}",
            f"git config --global --add safe.directory {shlex.quote(ctx.runtime.workspace_root + '/workspace/vllm')}",
            f"git config --global --add safe.directory {shlex.quote(ctx.runtime.workspace_root + '/workspace/vllm-ascend')}",
            "mkdir -p /root/.ssh",
            "chmod 700 /root/.ssh",
            "touch /root/.ssh/authorized_keys",
            "chmod 600 /root/.ssh/authorized_keys",
            f"grep -qxF {shlex.quote(public_key)} /root/.ssh/authorized_keys || printf '%s\\n' {shlex.quote(public_key)} >> /root/.ssh/authorized_keys",
            "if command -v sshd >/dev/null 2>&1 || [ -x /usr/sbin/sshd ]; then",
            "  mkdir -p /var/run/sshd",
            "  if [ -f /etc/ssh/sshd_config ] && ! grep -q '^Port "
            f"{ctx.runtime.ssh_port}$' /etc/ssh/sshd_config; then",
            f"    printf '\\nPort {ctx.runtime.ssh_port}\\nPermitRootLogin yes\\nPubkeyAuthentication yes\\nPasswordAuthentication yes\\n' >> /etc/ssh/sshd_config",
            "  fi",
            f"  (sshd -p {ctx.runtime.ssh_port} || /usr/sbin/sshd -p {ctx.runtime.ssh_port}) >/dev/null 2>&1 || true",
            "fi",
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


def _write_host_runtime_state(ctx: TargetContext, transport: str, current_session: Optional[str] = None) -> None:
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
    if current_session:
        runtime_state["current_session"] = current_session
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
    _stream_repo_to_host(paths, ctx)
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
        status=docker_exec_status,
        detail=docker_exec_detail,
    )
    if docker_exec_status == "ready":
        return VerificationResult.ready(
            summary=f"runtime ready for host {ctx.host.host} via docker exec",
            runtime=_verification_runtime_payload(
                ctx,
                transport="docker-exec",
                container_endpoint=(
                    f"docker-exec://{ctx.credential.username}@{ctx.host.host}/{ctx.runtime.container_name}"
                ),
            ),
            checks=[container_ssh_check, docker_exec_check],
        )

    return VerificationResult.needs_repair(
        summary=f"runtime unavailable for host {ctx.host.host}",
        runtime=_verification_runtime_payload(
            ctx,
            transport="unknown",
            container_endpoint=(
                f"docker-exec://{ctx.credential.username}@{ctx.host.host}/{ctx.runtime.container_name}"
            ),
        ),
        checks=[container_ssh_check, docker_exec_check],
    )


def verify_runtime(paths: RepoPaths, ctx: TargetContext) -> VerificationResult:
    if ctx.credential.mode == "local-simulation":
        return _verify_simulation_runtime(ctx)
    return _verify_host_runtime(ctx)


def create_remote_session(paths: RepoPaths, ctx: TargetContext, manifest: Dict[str, Any], transport: str) -> None:
    if ctx.credential.mode == "local-simulation":
        _ensure_simulation_session(paths, ctx, manifest)
        return

    session_name = manifest["name"]
    session_root = f"{ctx.runtime.workspace_root}/.vaws/sessions/{session_name}"
    manifest_text = yaml.safe_dump(manifest, sort_keys=False).rstrip()
    script = "\n".join(
        [
            "set -e",
            f"mkdir -p {shlex.quote(session_root)}",
            f"mkdir -p {shlex.quote(session_root + '/.venv')}",
            f"cat > {shlex.quote(session_root + '/manifest.yaml')} <<'EOF'",
            manifest_text,
            "EOF",
            f"if [ ! -e {shlex.quote(session_root + '/vllm/.git')} ] && [ ! -f {shlex.quote(session_root + '/vllm/.git')} ]; then",
            f"  rm -rf {shlex.quote(session_root + '/vllm')}",
            f"  git -C {shlex.quote(ctx.runtime.workspace_root + '/workspace/vllm')} worktree add --force -B {shlex.quote(manifest['vllm_ref']['branch'])} {shlex.quote(session_root + '/vllm')} {shlex.quote(manifest['vllm_ref']['base_ref'])}",
            "fi",
            f"if [ ! -e {shlex.quote(session_root + '/vllm-ascend/.git')} ] && [ ! -f {shlex.quote(session_root + '/vllm-ascend/.git')} ]; then",
            f"  rm -rf {shlex.quote(session_root + '/vllm-ascend')}",
            f"  git -C {shlex.quote(ctx.runtime.workspace_root + '/workspace/vllm-ascend')} worktree add --force -B {shlex.quote(manifest['vllm_ascend_ref']['branch'])} {shlex.quote(session_root + '/vllm-ascend')} {shlex.quote(manifest['vllm_ascend_ref']['base_ref'])}",
            "fi",
        ]
    )
    result = _run_container_command(ctx, transport, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to materialize remote session '{session_name}': {(result.stderr or result.stdout).strip()}"
        )


def switch_remote_session(ctx: TargetContext, session_name: str, transport: str) -> None:
    if ctx.credential.mode == "local-simulation":
        _switch_simulation_session(ctx, session_name)
        return

    session_root = f"{ctx.runtime.workspace_root}/.vaws/sessions/{session_name}"
    script = "\n".join(
        [
            "set -e",
            f"test -d {shlex.quote(session_root)}",
            f"ln -sfn {shlex.quote(session_root)} {shlex.quote(ctx.runtime.workspace_root + '/.vaws/current')}",
            f"rm -rf {shlex.quote(ctx.runtime.workspace_root + '/vllm')}",
            f"rm -rf {shlex.quote(ctx.runtime.workspace_root + '/vllm-ascend')}",
            f"ln -sfn {shlex.quote(ctx.runtime.workspace_root + '/.vaws/current/vllm')} {shlex.quote(ctx.runtime.workspace_root + '/vllm')}",
            f"ln -sfn {shlex.quote(ctx.runtime.workspace_root + '/.vaws/current/vllm-ascend')} {shlex.quote(ctx.runtime.workspace_root + '/vllm-ascend')}",
        ]
    )
    result = _run_container_command(ctx, transport, script)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to switch remote session '{session_name}': {(result.stderr or result.stdout).strip()}"
        )
    _write_host_runtime_state(ctx, transport, current_session=session_name)


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
