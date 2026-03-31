from __future__ import annotations

from typing import Any, Dict

import yaml

from .bootstrap import (
    DEFAULT_RUNTIME_BOOTSTRAP_MODE,
    DEFAULT_RUNTIME_CONTAINER,
    DEFAULT_RUNTIME_IMAGE,
    DEFAULT_SERVER_PORT,
    DEFAULT_RUNTIME_SSH_PORT,
    DEFAULT_RUNTIME_WORKSPACE_ROOT,
    DEFAULT_SERVER_AUTH_REF,
    DEFAULT_SERVER_STATUS,
)
from .config import RepoPaths
from .remote import (
    DEFAULT_HOST_WORKSPACE_BASE,
    RemoteError,
    ensure_runtime,
    persist_server_verification,
    resolve_server_context,
    verify_runtime,
)


class FleetError(RuntimeError):
    """Fleet inventory operation failure."""


def _read_servers_config(paths: RepoPaths) -> Dict[str, Any]:
    servers_file = paths.local_servers_file
    if not servers_file.is_file():
        raise FleetError("missing server inventory: run `vaws init --bootstrap` first")

    try:
        loaded = yaml.safe_load(servers_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise FleetError("invalid server config: .workspace.local/servers.yaml") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise FleetError("invalid server config: .workspace.local/servers.yaml must be a YAML mapping")
    return loaded


def _write_servers_config(paths: RepoPaths, config: Dict[str, Any]) -> None:
    paths.local_servers_file.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def _require_non_empty_string(value: Any, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FleetError(message)
    return value.strip()


def _require_int(value: Any, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FleetError(message)
    return value


def _require_port(value: Any, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 65535:
        raise FleetError(message)
    return value


def list_fleet(paths: RepoPaths) -> int:
    try:
        config = _read_servers_config(paths)
        servers = config.get("servers")
        if not isinstance(servers, dict):
            raise FleetError("invalid server config: missing 'servers' map")
    except (FleetError, RuntimeError) as exc:
        print(str(exc))
        return 1

    print("fleet list:")
    if not servers:
        print("  (no servers)")
        return 0

    for server_name in sorted(servers):
        server = servers[server_name]
        if not isinstance(server, dict):
            print(f"- {server_name}: invalid entry")
            continue
        status = server.get("status", "unknown")
        host = server.get("host", "?")
        port = server.get("port", "?")
        runtime = server.get("runtime")
        container_name = "?"
        if isinstance(runtime, dict):
            container_name = runtime.get("container_name", "?")
        print(f"- {server_name}: {status} {host}:{port} {container_name}")
    return 0


def add_fleet_server(
    paths: RepoPaths,
    server_name: str,
    server_host: str,
    ssh_auth_ref: str,
    server_user: str = "root",
    server_port: int = DEFAULT_SERVER_PORT,
    runtime_image: str = DEFAULT_RUNTIME_IMAGE,
    runtime_container: str = DEFAULT_RUNTIME_CONTAINER,
    runtime_ssh_port: int = DEFAULT_RUNTIME_SSH_PORT,
    runtime_workspace_root: str = DEFAULT_RUNTIME_WORKSPACE_ROOT,
    runtime_bootstrap_mode: str = DEFAULT_RUNTIME_BOOTSTRAP_MODE,
) -> int:
    try:
        server_name = _require_non_empty_string(server_name, "missing server name")
        server_host = _require_non_empty_string(server_host, "missing server host")
        server_user = _require_non_empty_string(server_user, "missing server user")
        ssh_auth_ref = _require_non_empty_string(ssh_auth_ref, "missing ssh auth ref")
        runtime_image = _require_non_empty_string(runtime_image, "missing runtime image")
        runtime_container = _require_non_empty_string(
            runtime_container,
            "missing runtime container",
        )
        runtime_workspace_root = _require_non_empty_string(
            runtime_workspace_root,
            "missing runtime workspace root",
        )
        runtime_bootstrap_mode = _require_non_empty_string(
            runtime_bootstrap_mode,
            "missing runtime bootstrap mode",
        )
        server_port = _require_port(server_port, "invalid server port")
        runtime_ssh_port = _require_port(runtime_ssh_port, "invalid runtime ssh port")

        config = _read_servers_config(paths)
        servers = config.get("servers")
        if not isinstance(servers, dict):
            raise FleetError("invalid server config: missing 'servers' map")

        servers[server_name] = {
            "host": server_host,
            "port": server_port,
            "login_user": server_user,
            "ssh_auth_ref": ssh_auth_ref,
            "status": DEFAULT_SERVER_STATUS,
            "runtime": {
                "image_ref": runtime_image,
                "container_name": runtime_container,
                "ssh_port": runtime_ssh_port,
                "workspace_root": runtime_workspace_root,
                "bootstrap_mode": runtime_bootstrap_mode,
                "host_workspace_path": f"{DEFAULT_HOST_WORKSPACE_BASE}/{server_name}/workspace",
            },
        }
        config["version"] = 1
        config["servers"] = servers
        _write_servers_config(paths, config)

        context = resolve_server_context(paths, server_name)
        ensure_runtime(paths, context)
        verification = verify_runtime(paths, context)
        persist_server_verification(paths, server_name, verification)
    except (FleetError, RuntimeError, OSError) as exc:
        print(str(exc))
        return 1

    print(f"fleet add: {verification.status} ({server_name})")
    return 0


def verify_fleet_server(paths: RepoPaths, server_name: str) -> int:
    try:
        context = resolve_server_context(paths, server_name)
        verification = verify_runtime(paths, context)
    except (RemoteError, RuntimeError) as exc:
        print(str(exc))
        return 1

    print(f"fleet verify: {verification.status} ({server_name})")
    return 0
