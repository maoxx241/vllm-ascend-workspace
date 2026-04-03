from __future__ import annotations

import json
import shlex
import time
import urllib.request
import uuid

from .config import RepoPaths
from .runtime_transport import (
    resolve_available_runtime_transport,
    run_container_command,
    run_detached_container_command,
)
from .serving_assets import MaterializedServingConfig, load_serving_preset, materialize_serving_config
from .serving_session import (
    create_service_session_record,
    load_service_session,
    remove_service_session_record,
    resolve_api_key,
    upsert_service_session_record,
)
from .target_context import resolve_server_context


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


def launch_service_session(
    paths: RepoPaths,
    *,
    server_name: str,
    preset_name: str,
    weights_path: str,
    api_key_env: str | None,
    lifecycle: str,
) -> dict[str, object]:
    preset = load_serving_preset(preset_name)
    config = materialize_serving_config(preset, weights_input=weights_path)
    ctx = resolve_server_context(paths, server_name)
    transport = resolve_available_runtime_transport(ctx)
    service_id = f"{server_name}-{preset_name}-{uuid.uuid4().hex[:8]}"
    log_path = f"/tmp/{service_id}.log"
    pid_path = f"/tmp/{service_id}.pid"
    command = build_single_node_replica_command(
        config,
        workspace_root=ctx.runtime.workspace_root,
        api_key_env=api_key_env,
    )
    launch = run_detached_container_command(
        ctx,
        transport,
        command,
        log_path=log_path,
        pid_path=pid_path,
    )
    service = create_service_session_record(
        paths,
        service_id=service_id,
        server_name=server_name,
        weights_path=weights_path,
        pid=(launch.stdout or "").strip(),
        pid_path=pid_path,
        log_path=log_path,
        api_key_env=api_key_env,
        config=config,
        ctx=ctx,
        transport=transport,
        lifecycle=lifecycle,
    )
    return upsert_service_session_record(paths, service)


def probe_service_readiness(
    paths: RepoPaths,
    service_id: str,
    *,
    timeout_s: float = 180.0,
) -> dict[str, object]:
    service = load_service_session(paths, service_id)
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
            return upsert_service_session_record(paths, service)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(2.0)
    raise RuntimeError(f"service readiness check failed: {last_error}")


def stop_service_session(paths: RepoPaths, service_id: str) -> dict[str, object]:
    service = load_service_session(paths, service_id)
    ctx = resolve_server_context(paths, str(service["server_name"]))
    transport = resolve_available_runtime_transport(ctx)
    pid_path = str(service["process"]["pid_path"])
    script = "\n".join(
        [
            "set -euo pipefail",
            f'if [ -f {shlex.quote(pid_path)} ]; then kill "$(cat {shlex.quote(pid_path)})" || true; fi',
            f"rm -f {shlex.quote(pid_path)}",
        ]
    )
    result = run_container_command(ctx, transport, script)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"failed to stop {service_id}")
    remove_service_session_record(paths, service_id)
    return service


def start_service_session(
    paths: RepoPaths,
    *,
    server_name: str,
    preset_name: str,
    weights_path: str,
    api_key_env: str | None,
    lifecycle: str,
) -> str:
    service = launch_service_session(
        paths,
        server_name=server_name,
        preset_name=preset_name,
        weights_path=weights_path,
        api_key_env=api_key_env,
        lifecycle=lifecycle,
    )
    probe_service_readiness(paths, str(service["service_id"]))
    return str(service["service_id"])
