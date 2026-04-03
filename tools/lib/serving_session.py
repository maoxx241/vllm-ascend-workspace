from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .config import RepoPaths
from .machine_registry import list_server_records
from .remote_types import TargetContext
from .repo_targets import resolve_repo_targets
from .runtime_transport import resolve_available_runtime_transport
from .service_manifest import list_service_manifests, remote_service_manifest_path, write_service_manifest
from .serving_assets import MaterializedServingConfig
from .target_context import resolve_server_context


def current_code_fingerprint(paths: RepoPaths, server_name: str) -> dict[str, str]:
    del server_name
    targets = resolve_repo_targets(
        paths,
        vllm_upstream_tag=None,
        vllm_upstream_branch=None,
        vllm_ascend_upstream_branch=None,
    )
    return {
        "workspace": targets.workspace.commit,
        "vllm": targets.vllm.commit,
        "vllm_ascend": targets.vllm_ascend.commit,
    }


def resolve_api_key(auth_ref: object) -> str | None:
    if not isinstance(auth_ref, str):
        return None
    if not auth_ref.startswith("env:"):
        raise RuntimeError(f"unsupported auth ref: {auth_ref}")
    return os.environ.get(auth_ref.removeprefix("env:"))


def _read_server_service_manifests(paths: RepoPaths, server_name: str) -> list[dict[str, object]]:
    ctx = resolve_server_context(paths, server_name)
    transport = resolve_available_runtime_transport(ctx)
    payloads = list_service_manifests(ctx, transport)
    results: list[dict[str, object]] = []
    for payload in payloads:
        record = dict(payload)
        record.setdefault("server_name", server_name)
        results.append(record)
    return results


def _read_all_service_manifests(paths: RepoPaths) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for server_name in sorted(list_server_records(paths)):
        try:
            results.extend(_read_server_service_manifests(paths, server_name))
        except RuntimeError:
            continue
    results.sort(key=lambda payload: str(payload.get("service_id", "")))
    return results


def load_service_session(paths: RepoPaths, service_id: str) -> dict[str, object]:
    for payload in _read_all_service_manifests(paths):
        if str(payload.get("service_id")) == service_id:
            return dict(payload)
    raise RuntimeError(f"unknown service_id: {service_id}")


def describe_service_session(paths: RepoPaths, service_id: str) -> dict[str, object]:
    return load_service_session(paths, service_id)


def list_service_sessions(paths: RepoPaths) -> list[dict[str, object]]:
    return sorted(
        _read_all_service_manifests(paths),
        key=lambda payload: str(payload.get("service_id", "")),
    )


def upsert_service_session_record(paths: RepoPaths, session: dict[str, Any]) -> dict[str, object]:
    server_name = str(session.get("server_name", ""))
    if not server_name:
        raise RuntimeError("service session is missing server_name")
    ctx = resolve_server_context(paths, server_name)
    transport = resolve_available_runtime_transport(ctx)
    record = dict(session)
    record["manifest_path"] = write_service_manifest(ctx, transport, record)
    return record


def remove_service_session_record(paths: RepoPaths, service_id: str) -> None:
    service = load_service_session(paths, service_id)
    ctx = resolve_server_context(paths, str(service["server_name"]))
    transport = resolve_available_runtime_transport(ctx)
    from .service_manifest import remove_service_manifest

    remove_service_manifest(ctx, transport, service_id)


def create_service_session_record(
    paths: RepoPaths,
    *,
    service_id: str,
    server_name: str,
    weights_path: str,
    pid: str,
    pid_path: str,
    log_path: str,
    api_key_env: str | None,
    config: MaterializedServingConfig,
    ctx: TargetContext,
    transport: str,
    lifecycle: str,
) -> dict[str, object]:
    manifest_path = remote_service_manifest_path(ctx.runtime.workspace_root, service_id)
    return {
        "service_id": service_id,
        "server_name": server_name,
        "manifest_path": manifest_path,
        "topology": config.topology,
        "model_profile": config.model_profile,
        "bind_host": "0.0.0.0",
        "bind_port": config.port,
        "reachable_host": ctx.host.host,
        "reachable_port": config.port,
        "server_root_url": f"http://{ctx.host.host}:{config.port}",
        "openai_api_base_url": f"http://{ctx.host.host}:{config.port}/v1",
        "openai_api_auth_ref": f"env:{api_key_env}" if api_key_env else None,
        "served_model_names": [config.served_model_name],
        "primary_served_model_name": config.served_model_name,
        "effective_serve_args": dict(config.serve_args),
        "device_binding": config.device_binding,
        "weights_input": weights_path,
        "loaded_model_source": config.weights_input,
        "code_fingerprint": current_code_fingerprint(paths, server_name),
        "runtime_fingerprint": {"transport": transport},
        "health_status": "starting",
        "lifecycle": lifecycle,
        "process": {"pid": pid, "pid_path": pid_path, "log_path": log_path},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
