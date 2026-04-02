from __future__ import annotations

import shlex
import time
import uuid

from .benchmark_assets import BenchmarkPreset, load_benchmark_preset
from .capability_state import record_benchmark_run
from .config import RepoPaths
from .remote import resolve_server_context, run_runtime_command
from .runtime import require_container_ssh_transport
from .serving import (
    current_code_fingerprint,
    load_service_session,
    start_service_session,
    stop_service_by_id,
)


def start_temporary_service(
    paths: RepoPaths,
    *,
    server_name: str,
    serving_preset_name: str,
    weights_path: str,
    lifecycle: str,
) -> dict[str, object]:
    service_id = start_service_session(
        paths,
        server_name=server_name,
        preset_name=serving_preset_name,
        weights_path=weights_path,
        api_key_env=None,
        lifecycle=lifecycle,
    )
    return load_service_session(paths, service_id)


def service_is_reusable(paths: RepoPaths, server_name: str, service: dict[str, object]) -> bool:
    expected = current_code_fingerprint(paths, server_name)
    return service.get("code_fingerprint") == expected and service.get("server_name") == server_name


def run_benchmark_probe(paths: RepoPaths, preset: BenchmarkPreset, service: dict[str, object]) -> dict[str, str]:
    ctx = resolve_server_context(paths, str(service["server_name"]))
    transport = require_container_ssh_transport(paths, str(service["server_name"]))
    workspace_root = ctx.runtime.workspace_root
    wrapper_path = f"{workspace_root}/workspace/benchmarking/probes/performance/vllm-bench-serve.py"
    result_json = f"/tmp/{service['service_id']}-{preset.name}.json"
    command = "\n".join(
        [
            "set -euo pipefail",
            f"cd {shlex.quote(f'{workspace_root}/workspace')}",
            "source /usr/local/Ascend/ascend-toolkit/set_env.sh",
            "source /usr/local/Ascend/nnal/atb/set_env.sh",
            (
                "source "
                + shlex.quote(
                    f"{workspace_root}/workspace/vllm-ascend/"
                    "vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash"
                )
            ),
            "export PATH=/usr/local/python3.11.14/bin:$PATH",
            "/usr/local/python3.11.14/bin/python3 "
            + " ".join(
                [
                    shlex.quote(wrapper_path),
                    "--base-url",
                    shlex.quote(str(service["server_root_url"])),
                    "--model",
                    shlex.quote(str(service["primary_served_model_name"])),
                    "--dataset-name",
                    shlex.quote(preset.dataset_name),
                    "--num-prompts",
                    str(preset.num_prompts),
                    "--random-input-len",
                    str(preset.random_input_len),
                    "--random-output-len",
                    str(preset.random_output_len),
                    "--result-json",
                    shlex.quote(result_json),
                ]
            ),
        ]
    )
    result = run_runtime_command(ctx, transport, command)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    summary = run_runtime_command(ctx, transport, f"cat {shlex.quote(result_json)}").stdout.strip()
    return {"result_path": result_json, "summary": summary}


def run_benchmark(
    paths: RepoPaths,
    server_name: str,
    preset_name: str,
    weights_path: str | None,
    service_id: str | None,
) -> int:
    preset = load_benchmark_preset(preset_name)
    owns_service = service_id is None
    if owns_service:
        if not weights_path:
            print("weights path is required when benchmark creates an ephemeral service")
            return 1
        service = start_temporary_service(
            paths,
            server_name=server_name,
            serving_preset_name=preset.serving_preset,
            weights_path=weights_path,
            lifecycle="benchmark-temporary",
        )
    else:
        service = load_service_session(paths, service_id)
        if not service_is_reusable(paths, server_name, service):
            print("service fingerprint does not match current workspace state")
            return 1
    try:
        result = run_benchmark_probe(paths, preset, service)
        run_id = f"{preset.name}-{uuid.uuid4().hex[:8]}"
        record_benchmark_run(
            paths,
            run_id,
            {
                "preset_name": preset.name,
                "service_id": service["service_id"],
                "server_name": service["server_name"],
                "result_path": result["result_path"],
                "summary": result["summary"],
                "finished_at": int(time.time()),
            },
        )
        print(result["summary"])
        return 0
    finally:
        if owns_service:
            stop_service_by_id(paths, str(service["service_id"]))
