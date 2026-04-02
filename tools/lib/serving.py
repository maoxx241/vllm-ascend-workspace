from __future__ import annotations

import json
import os
import shlex
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from .benchmark_assets import BenchmarkPreset
from .capability_state import (
    read_capability_state,
    remove_service_session,
    upsert_service_session,
)
from .config import RepoPaths
from .remote import (
    TargetContext,
    resolve_server_context,
    run_detached_runtime_command,
    run_runtime_command,
)
from .runtime import require_container_ssh_transport
from .serving_assets import MaterializedServingConfig, load_serving_preset, materialize_serving_config


def _serve_arg_cli_tokens(serve_args: dict[str, object]) -> list[str]:
    tokens: list[str] = []
    for key, value in serve_args.items():
        flag = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                tokens.append(flag)
            continue
        if value is None:
            continue
        rendered = json.dumps(value, separators=(",", ":")) if isinstance(value, (dict, list)) else str(value)
        tokens.extend([flag, rendered])
    return tokens


def build_single_node_replica_command(
    config: MaterializedServingConfig,
    *,
    workspace_root: str,
    api_key_env: str | None = None,
) -> str:
    additional_config_json = json.dumps(config.additional_config, separators=(",", ":"))
    env_script_path = shlex.quote(
        f"{workspace_root}/workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash"
    )
    serve_parts = [
        "set -euo pipefail",
        "source /usr/local/Ascend/ascend-toolkit/set_env.sh",
        "source /usr/local/Ascend/nnal/atb/set_env.sh",
        f"source {env_script_path}",
        "export PATH=/usr/local/python3.11.14/bin:$PATH",
        "export PYTHON=/usr/local/python3.11.14/bin/python3",
        "export PIP=/usr/local/python3.11.14/bin/pip",
        f"export ASCEND_RT_VISIBLE_DEVICES={shlex.quote(config.device_binding)}",
        "export VLLM_WORKER_MULTIPROC_METHOD=spawn",
        "export OMP_NUM_THREADS=1",
        "export MKL_NUM_THREADS=1",
    ]
    if api_key_env:
        serve_parts.append(f'export VLLM_API_KEY="${{{api_key_env}}}"')
    serve_parts.append(
        " ".join(
            [
                "/usr/local/python3.11.14/bin/python3",
                "-m",
                "vllm.entrypoints.cli.main",
                "serve",
                shlex.quote(config.weights_input),
                "--host",
                "0.0.0.0",
                "--port",
                str(config.port),
                "--served-model-name",
                shlex.quote(config.served_model_name),
                "--additional-config",
                shlex.quote(additional_config_json),
            ]
            + _serve_arg_cli_tokens(config.serve_args)
            + (["--api-key", '"$VLLM_API_KEY"'] if api_key_env else [])
        )
    )
    return "\n".join(serve_parts)


def current_code_fingerprint(paths: RepoPaths, server_name: str) -> dict[str, str]:
    state = read_capability_state(paths)
    runtime_env = state.get("runtime_environment", {}).get(server_name, {})
    if isinstance(runtime_env, dict) and runtime_env:
        return {
            "workspace": str(runtime_env.get("workspace_commit", "")),
            "vllm": str(runtime_env.get("vllm_commit", "")),
            "vllm_ascend": str(runtime_env.get("vllm_ascend_commit", "")),
        }
    code_parity = state.get("code_parity", {}).get(server_name, {})
    desired_state = code_parity.get("desired_state", {}) if isinstance(code_parity, dict) else {}
    return {
        "workspace": str(desired_state.get("workspace", {}).get("commit", "")),
        "vllm": str(desired_state.get("vllm", {}).get("commit", "")),
        "vllm_ascend": str(desired_state.get("vllm-ascend", {}).get("commit", "")),
    }


def resolve_api_key(auth_ref: object) -> str | None:
    if not isinstance(auth_ref, str):
        return None
    if not auth_ref.startswith("env:"):
        raise RuntimeError(f"unsupported auth ref: {auth_ref}")
    return os.environ.get(auth_ref.removeprefix("env:"))


def load_service_session(paths: RepoPaths, service_id: str) -> dict[str, object]:
    state = read_capability_state(paths)
    services = state.get("services", {})
    if not isinstance(services, dict) or service_id not in services:
        raise RuntimeError(f"unknown service_id: {service_id}")
    payload = services[service_id]
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid service payload: {service_id}")
    return dict(payload)


def create_service_session_record(
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
    lifecycle: str,
) -> dict[str, object]:
    return {
        "service_id": service_id,
        "server_name": server_name,
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
        "runtime_fingerprint": {"transport": "container-ssh"},
        "health_status": "starting",
        "lifecycle": lifecycle,
        "process": {"pid": pid, "pid_path": pid_path, "log_path": log_path},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def validate_service_readiness(service: dict[str, object], timeout_s: float = 180.0) -> None:
    root_url = str(service["server_root_url"])
    model_name = str(service["primary_served_model_name"])
    api_key = resolve_api_key(service.get("openai_api_auth_ref"))
    deadline = time.monotonic() + timeout_s
    last_error = "service did not become ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{root_url}/health", timeout=2):
                pass
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                f"{root_url}/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10):
                pass
            service["health_status"] = "ready"
            return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(2.0)
    raise RuntimeError(f"service readiness check failed: {last_error}")


def list_services(paths: RepoPaths) -> int:
    state = read_capability_state(paths)
    for service_id, payload in sorted(state.get("services", {}).items()):
        if isinstance(payload, dict):
            print(
                f"{service_id}\t{payload['server_name']}\t"
                f"{payload['primary_served_model_name']}\t{payload['lifecycle']}"
            )
    return 0


def status_service(paths: RepoPaths, service_id: str) -> int:
    service = load_service_session(paths, service_id)
    print(json.dumps(service, indent=2, sort_keys=True))
    return 0


def stop_service(paths: RepoPaths, service_id: str) -> int:
    return stop_service_by_id(paths, service_id)


def stop_service_by_id(paths: RepoPaths, service_id: str) -> int:
    service = load_service_session(paths, service_id)
    ctx = resolve_server_context(paths, str(service["server_name"]))
    transport = require_container_ssh_transport(paths, str(service["server_name"]))
    pid_path = str(service["process"]["pid_path"])
    script = "\n".join(
        [
            "set -euo pipefail",
            f'if [ -f {shlex.quote(pid_path)} ]; then kill "$(cat {shlex.quote(pid_path)})" || true; fi',
            f"rm -f {shlex.quote(pid_path)}",
        ]
    )
    run_runtime_command(ctx, transport, script)
    remove_service_session(paths, service_id)
    return 0


def start_service_session(
    paths: RepoPaths,
    *,
    server_name: str,
    preset_name: str,
    weights_path: str,
    api_key_env: str | None,
    lifecycle: str,
) -> str:
    preset = load_serving_preset(preset_name)
    config = materialize_serving_config(preset, weights_input=weights_path)
    ctx = resolve_server_context(paths, server_name)
    transport = require_container_ssh_transport(paths, server_name)
    service_id = f"{server_name}-{preset_name}-{uuid.uuid4().hex[:8]}"
    log_path = f"/tmp/{service_id}.log"
    pid_path = f"/tmp/{service_id}.pid"
    command = build_single_node_replica_command(
        config,
        workspace_root=ctx.runtime.workspace_root,
        api_key_env=api_key_env,
    )
    launch = run_detached_runtime_command(
        ctx,
        transport,
        command,
        log_path=log_path,
        pid_path=pid_path,
    )
    service = create_service_session_record(
        service_id=service_id,
        server_name=server_name,
        weights_path=weights_path,
        pid=(launch.stdout or "").strip(),
        pid_path=pid_path,
        log_path=log_path,
        api_key_env=api_key_env,
        config=config,
        ctx=ctx,
        lifecycle=lifecycle,
    )
    validate_service_readiness(service)
    upsert_service_session(paths, service)
    return service_id


def start_service(
    paths: RepoPaths,
    server_name: str,
    preset_name: str,
    weights_path: str,
    api_key_env: str | None = None,
) -> int:
    service_id = start_service_session(
        paths,
        server_name=server_name,
        preset_name=preset_name,
        weights_path=weights_path,
        api_key_env=api_key_env,
        lifecycle="explicit-serving",
    )
    print(service_id)
    return 0
