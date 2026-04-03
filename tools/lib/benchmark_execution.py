from __future__ import annotations

import shlex
import time
import uuid
from typing import Any

from .benchmark_assets import BenchmarkPreset, load_benchmark_preset
from .benchmark_run_artifacts import (
    list_benchmark_run_artifacts,
    load_benchmark_run_artifact,
    write_benchmark_run_artifact,
)
from .config import RepoPaths
from .runtime_transport import resolve_available_runtime_transport, run_container_command
from .serving_session import current_code_fingerprint, load_service_session
from .target_context import resolve_server_context


def describe_benchmark_preset(preset_name: str) -> dict[str, object]:
    preset = load_benchmark_preset(preset_name)
    return {
        "preset_name": preset.name,
        "serving_preset": preset.serving_preset,
        "probe_runner": preset.probe_runner,
        "request_family": preset.request_family,
        "dataset_name": preset.dataset_name,
        "num_prompts": preset.num_prompts,
        "random_input_len": preset.random_input_len,
        "random_output_len": preset.random_output_len,
    }


def describe_benchmark_run(paths: RepoPaths, run_id: str | None = None) -> dict[str, object]:
    if run_id is None:
        ranked = list_benchmark_run_artifacts(paths)
        if not ranked:
            raise RuntimeError("no benchmark runs recorded")
        return dict(ranked[0])
    return load_benchmark_run_artifact(paths, run_id)


def service_is_reusable(paths: RepoPaths, server_name: str, service: dict[str, object]) -> bool:
    expected = current_code_fingerprint(paths, server_name)
    return service.get("code_fingerprint") == expected and service.get("server_name") == server_name


def run_benchmark_probe(
    paths: RepoPaths,
    *,
    server_name: str,
    preset_name: str,
    service_id: str,
) -> dict[str, object]:
    service = load_service_session(paths, service_id)
    if not service_is_reusable(paths, server_name, service):
        raise RuntimeError("service fingerprint does not match current workspace state")

    preset = load_benchmark_preset(preset_name)
    ctx = resolve_server_context(paths, str(service["server_name"]))
    transport = resolve_available_runtime_transport(ctx)
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
    result = run_container_command(ctx, transport, command)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())

    summary_result = run_container_command(ctx, transport, f"cat {shlex.quote(result_json)}")
    if summary_result.returncode != 0:
        raise RuntimeError((summary_result.stderr or summary_result.stdout).strip())
    summary = summary_result.stdout.strip()

    run_id = f"{preset.name}-{uuid.uuid4().hex[:8]}"
    record = {
        "preset_name": preset.name,
        "service_id": service["service_id"],
        "server_name": service["server_name"],
        "result_path": result_json,
        "summary": summary,
        "finished_at": int(time.time()),
    }
    artifact_path = write_benchmark_run_artifact(paths, run_id, record)

    payload: dict[str, Any] = dict(record)
    payload["run_id"] = run_id
    payload["artifact_path"] = str(artifact_path)
    return payload
