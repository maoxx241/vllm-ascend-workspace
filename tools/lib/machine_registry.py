from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import yaml

from .config import RepoPaths


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_servers_config(paths: RepoPaths) -> Dict[str, Any]:
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


def write_servers_config(paths: RepoPaths, config: Dict[str, Any]) -> None:
    paths.local_servers_file.parent.mkdir(parents=True, exist_ok=True)
    paths.local_servers_file.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def list_server_records(paths: RepoPaths) -> Dict[str, Any]:
    config = read_servers_config(paths)
    servers = config.get("servers", {})
    return servers if isinstance(servers, dict) else {}


def describe_server_record(paths: RepoPaths, server_name: str) -> Dict[str, Any]:
    record = list_server_records(paths).get(server_name)
    if not isinstance(record, dict):
        raise RuntimeError(f"unknown server: {server_name}")
    return dict(record)


def build_server_record(
    server_name: str,
    server_host: str,
    *,
    ssh_auth_ref: str,
    server_user: str,
    server_port: int,
    runtime_image: str,
    runtime_container: str,
    runtime_ssh_port: int,
    runtime_workspace_root: str,
    runtime_bootstrap_mode: str,
    host_workspace_base: str,
) -> Dict[str, Any]:
    return {
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
            "host_workspace_path": f"{host_workspace_base}/{server_name}/workspace",
        },
    }


def upsert_server_record(paths: RepoPaths, server_name: str, record: Dict[str, Any]) -> None:
    config = read_servers_config(paths)
    config.setdefault("servers", {})[server_name] = dict(record)
    write_servers_config(paths, config)


def update_server_status(paths: RepoPaths, server_name: str, status: str) -> None:
    config = read_servers_config(paths)
    server = config.setdefault("servers", {}).get(server_name)
    if isinstance(server, dict):
        server["status"] = status
    write_servers_config(paths, config)


def remove_server_record(paths: RepoPaths, server_name: str) -> None:
    config = read_servers_config(paths)
    config.setdefault("servers", {}).pop(server_name, None)
    write_servers_config(paths, config)


def verification_status(verification: Any) -> str:
    if isinstance(verification, dict):
        status = verification.get("status")
    else:
        status = getattr(verification, "status", None)
    if not isinstance(status, str) or not status.strip():
        raise RuntimeError("invalid runtime verification result")
    return status.strip()


def verification_summary(verification: Any) -> str:
    if isinstance(verification, dict):
        summary = verification.get("summary") or verification.get("detail")
    else:
        summary = getattr(verification, "summary", None)
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return "runtime verification completed"


def record_verification_observation(paths: RepoPaths, server_name: str, verification: Any) -> None:
    status = verification_status(verification)
    summary = verification_summary(verification)
    config = read_servers_config(paths)
    server_state = config.setdefault("servers", {}).setdefault(server_name, {})
    if not isinstance(server_state, dict):
        server_state = {}
        config["servers"][server_name] = server_state
    server_state["status"] = status
    server_state["last_verification"] = {
        "status": status,
        "detail": summary,
        "observed_at": _observed_at(),
        "evidence_source": "machine-management",
    }
    write_servers_config(paths, config)


def clear_server_observation(paths: RepoPaths, server_name: str) -> None:
    config = read_servers_config(paths)
    server_state = config.setdefault("servers", {}).get(server_name)
    if isinstance(server_state, dict):
        server_state.pop("last_verification", None)
    write_servers_config(paths, config)
