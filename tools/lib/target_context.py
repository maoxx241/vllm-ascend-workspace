from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import RepoPaths
from .remote_types import CredentialGroup, HostSpec, RemoteError, RuntimeSpec, TargetContext

DEFAULT_HOST_WORKSPACE_BASE = "/root/.vaws/targets"
DEFAULT_WORKSPACE_ROOT = "/vllm-workspace"


def _read_yaml_mapping(path: Path, invalid_message: str) -> dict[str, Any]:
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


def load_auth_config(paths: RepoPaths) -> dict[str, Any]:
    return _read_yaml_mapping(
        paths.local_overlay / "auth.yaml",
        "invalid auth config: .workspace.local/auth.yaml",
    )


def load_servers_config(paths: RepoPaths) -> dict[str, Any]:
    return _read_yaml_mapping(
        paths.local_servers_file,
        "invalid server config: .workspace.local/servers.yaml",
    )


def _host_ssh_auth_ref(host_config: dict[str, Any]) -> str | None:
    auth_ref = host_config.get("ssh_auth_ref")
    if isinstance(auth_ref, str) and auth_ref.strip():
        return auth_ref.strip()
    return None


def _modern_auth_ref_names(auth: dict[str, Any]) -> list[str]:
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
    ref: dict[str, Any],
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
    simulation_root_path: Path | None = None
    if kind == "local-simulation":
        if not isinstance(simulation_root, str) or not simulation_root.strip():
            raise RemoteError(
                f"invalid auth config: ssh_auth ref '{ref_name}' missing simulation_root"
            )
        raise RemoteError(
            "local-simulation auth refs are test-only and no longer supported by production target resolution"
        )

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
    auth: dict[str, Any],
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
    host_config: dict[str, Any],
    runtime: dict[str, Any],
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

    auth = load_auth_config(paths)
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


def resolve_server_context(paths: RepoPaths, server_name: str) -> TargetContext:
    config = load_servers_config(paths)
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


def list_managed_server_names(paths: RepoPaths) -> list[str]:
    if not paths.local_servers_file.exists():
        return []
    config = load_servers_config(paths)
    servers = config.get("servers")
    if not isinstance(servers, dict):
        raise RemoteError("invalid server config: missing 'servers' map")
    return sorted(
        name.strip()
        for name in servers
        if isinstance(name, str) and name.strip()
    )
