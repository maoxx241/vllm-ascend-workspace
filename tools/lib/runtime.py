from typing import Any, Dict

from .capability_state import (
    read_capability_state,
    write_capability_state,
)
from .config import RepoPaths


def read_state(paths: RepoPaths) -> Dict[str, Any]:
    return read_capability_state(paths)


def ensure_state_schema(paths: RepoPaths) -> Dict[str, Any]:
    return read_capability_state(paths)


def write_state(paths: RepoPaths, state: Dict[str, Any]) -> None:
    write_capability_state(paths, state)


def update_state(paths: RepoPaths, **updates: Any) -> Dict[str, Any]:
    state = read_state(paths)
    state.update(updates)
    write_state(paths, state)
    return state


def require_container_ssh_transport(paths: RepoPaths, server_name: str) -> str:
    state = read_capability_state(paths)
    servers = state.get("servers")
    server = servers.get(server_name) if isinstance(servers, dict) else None
    container_access = server.get("container_access") if isinstance(server, dict) else None
    if isinstance(container_access, dict) and container_access.get("status") == "ready":
        return "container-ssh"
    detail = None
    if isinstance(container_access, dict):
        detail = container_access.get("detail")
    message = f"container_ssh not ready for {server_name}"
    if isinstance(detail, str) and detail.strip():
        message = f"{message}: {detail.strip()}"
    raise RuntimeError(message)
