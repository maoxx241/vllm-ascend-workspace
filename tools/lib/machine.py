from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import yaml

from .capability_state import read_capability_state, write_capability_state
from .config import RepoPaths
from .remote import DEFAULT_HOST_WORKSPACE_BASE, ensure_runtime, resolve_server_context, verify_runtime
from .reset import cleanup_server_runtime

DEFAULT_SERVER_PORT = 22
DEFAULT_RUNTIME_IMAGE = "quay.nju.edu.cn/ascend/vllm-ascend:latest"
DEFAULT_RUNTIME_CONTAINER = "vaws-workspace"
DEFAULT_RUNTIME_SSH_PORT = 63269
DEFAULT_RUNTIME_WORKSPACE_ROOT = "/vllm-workspace"
DEFAULT_RUNTIME_BOOTSTRAP_MODE = "host-then-container"


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_servers_config(paths: RepoPaths) -> Dict[str, Any]:
    if not paths.local_servers_file.exists():
        return {"version": 1, "servers": {}}
    payload = yaml.safe_load(paths.local_servers_file.read_text(encoding="utf-8"))
    if payload is None:
        return {"version": 1, "servers": {}}
    if not isinstance(payload, dict):
        raise RuntimeError("invalid server config: .workspace.local/servers.yaml")
    payload.setdefault("version", 1)
    payload.setdefault("servers", {})
    return payload


def _write_servers_config(paths: RepoPaths, config: Dict[str, Any]) -> None:
    paths.local_servers_file.parent.mkdir(parents=True, exist_ok=True)
    paths.local_servers_file.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def _verification_status(verification: Any) -> str:
    if isinstance(verification, dict):
        status = verification.get("status")
    else:
        status = getattr(verification, "status", None)
    if not isinstance(status, str) or not status.strip():
        raise RuntimeError("invalid runtime verification result")
    return status.strip()


def _verification_summary(verification: Any) -> str:
    if isinstance(verification, dict):
        summary = verification.get("summary") or verification.get("detail")
    else:
        summary = getattr(verification, "summary", None)
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return "runtime verification completed"


def list_machines(paths: RepoPaths) -> int:
    config = _read_servers_config(paths)
    servers = config.get("servers", {})
    print("machine list:")
    if not isinstance(servers, dict) or not servers:
        print("  (no servers)")
        return 0
    for server_name, server in sorted(servers.items()):
        if not isinstance(server, dict):
            continue
        runtime = server.get("runtime")
        container_name = runtime.get("container_name") if isinstance(runtime, dict) else "?"
        print(f"- {server_name}: {server.get('host', '?')}:{server.get('port', '?')} {container_name}")
    return 0


def _record_verification_state(paths: RepoPaths, server_name: str, verification: Any) -> None:
    status = _verification_status(verification)
    summary = _verification_summary(verification)
    state = read_capability_state(paths)
    server_state = state.setdefault("servers", {}).setdefault(server_name, {})
    if not isinstance(server_state, dict):
        server_state = {}
        state["servers"][server_name] = server_state
    server_state["container_access"] = {
        "status": status,
        "mode": "ssh-key",
        "detail": summary,
        "observed_at": _observed_at(),
        "evidence_source": "machine-management",
    }
    if status == "ready":
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
    config = _read_servers_config(paths)
    config.setdefault("servers", {})[server_name] = {
        "host": server_host,
        "port": server_port,
        "login_user": server_user,
        "ssh_auth_ref": ssh_auth_ref,
        "status": "pending",
        "runtime": {
            "image_ref": runtime_image,
            "container_name": runtime_container,
            "ssh_port": runtime_ssh_port,
            "workspace_root": runtime_workspace_root,
            "bootstrap_mode": runtime_bootstrap_mode,
            "host_workspace_path": f"{DEFAULT_HOST_WORKSPACE_BASE}/{server_name}/workspace",
        },
    }
    _write_servers_config(paths, config)
    return verify_machine(paths, server_name)


def verify_machine(paths: RepoPaths, server_name: str) -> int:
    context = resolve_server_context(paths, server_name)
    ensure_runtime(paths, context)
    verification = verify_runtime(paths, context)
    status = _verification_status(verification)

    config = _read_servers_config(paths)
    server = config.setdefault("servers", {}).get(server_name)
    if isinstance(server, dict):
        server["status"] = status
    _write_servers_config(paths, config)
    _record_verification_state(paths, server_name, verification)

    if status != "ready":
        print(f"machine verify: {status} ({server_name})")
        return 1
    print(f"machine verify: ready ({server_name})")
    return 0


def remove_machine(paths: RepoPaths, server_name: str) -> int:
    partial_cleanup = False
    try:
        cleanup_server_runtime(paths, server_name)
    except Exception as exc:
        partial_cleanup = True
        print(f"machine remove: partial cleanup for {server_name}: {exc}")

    config = _read_servers_config(paths)
    config.setdefault("servers", {}).pop(server_name, None)
    _write_servers_config(paths, config)

    state = read_capability_state(paths)
    state.setdefault("servers", {}).pop(server_name, None)
    if state.get("current_server") == server_name:
        state.pop("current_server", None)
        state.pop("current_session", None)
    write_capability_state(paths, state)

    if partial_cleanup:
        return 1
    print(f"machine remove: ok ({server_name})")
    return 0
