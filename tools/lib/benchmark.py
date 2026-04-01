from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from .config import RepoPaths
from .remote import resolve_server_context, run_runtime_command
from .runtime import read_state, update_state


@dataclass(frozen=True)
class BenchmarkPreset:
    name: str
    script_path: Path
    runbook_path: Path
    model_path: str
    tensor_parallel_size: int
    visible_devices: str
    result_markers: Tuple[str, str, str, str]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


PRESETS: Dict[str, BenchmarkPreset] = {
    "qwen3-35b-tp4": BenchmarkPreset(
        name="qwen3-35b-tp4",
        script_path=_repo_root()
        / "benchmarking"
        / "serving"
        / "gdn"
        / "qwen3_5"
        / "qwen35_gdn_chunk_perf.py",
        runbook_path=_repo_root()
        / "benchmarking"
        / "serving"
        / "gdn"
        / "qwen3_5"
        / "RUNBOOK.md",
        model_path="/home/weights/Qwen3.5-35B-A3B",
        tensor_parallel_size=4,
        visible_devices="0,1,2,3",
        result_markers=(
            "JSON_RESULTS_BEGIN",
            "JSON_RESULTS_END",
            "MARKDOWN_ROWS_BEGIN",
            "MARKDOWN_ROWS_END",
        ),
    )
}


def get_benchmark_preset(name: str) -> BenchmarkPreset:
    try:
        return PRESETS[name]
    except KeyError as exc:
        raise RuntimeError(f"unknown benchmark preset: {name}") from exc


def _remote_asset_path(local_path: Path, workspace_root: str) -> str:
    relative_path = local_path.relative_to(_repo_root())
    return f"{workspace_root}/workspace/{relative_path.as_posix()}"


def build_benchmark_command(
    preset: BenchmarkPreset,
    *,
    workspace_root: str,
    visible_devices: str,
    output_path: str,
) -> str:
    remote_script_path = _remote_asset_path(preset.script_path, workspace_root)
    return "\n".join(
        [
            "set -euo pipefail",
            f"cd {workspace_root}/workspace",
            "source /usr/local/Ascend/ascend-toolkit/set_env.sh",
            "source /usr/local/Ascend/nnal/atb/set_env.sh",
            f"source {workspace_root}/workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash",
            "export PATH=/usr/local/python3.11.14/bin:$PATH",
            "export PYTHON=/usr/local/python3.11.14/bin/python3",
            "export PIP=/usr/local/python3.11.14/bin/pip",
            "export VLLM_WORKER_MULTIPROC_METHOD=spawn",
            "export OMP_NUM_THREADS=1",
            "export MKL_NUM_THREADS=1",
            f"export ASCEND_RT_VISIBLE_DEVICES={visible_devices}",
            "cd /vllm-workspace/workspace/vllm-ascend",
            f"/usr/local/python3.11.14/bin/python3 {remote_script_path} --model-path {preset.model_path} >{output_path} 2>&1",
        ]
    )


def _current_runtime_transport(paths: RepoPaths, server_name: str) -> str:
    state = read_state(paths)
    current_target = state.get("current_target")
    runtime = state.get("runtime")
    if current_target == server_name and isinstance(runtime, dict):
        transport = runtime.get("transport")
        if isinstance(transport, str) and transport.strip():
            return transport.strip()
    return "docker-exec"


def _extract_remote_marker_block(paths: RepoPaths, server_name: str, output_path: str) -> str:
    ctx = resolve_server_context(paths, server_name)
    transport = _current_runtime_transport(paths, server_name)
    script = (
        f"sed -n '/MARKDOWN_ROWS_BEGIN/,/MARKDOWN_ROWS_END/p' {output_path}"
    )
    result = run_runtime_command(ctx, transport, script)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def run_benchmark_preset(paths: RepoPaths, server_name: str, preset_name: str) -> int:
    preset = get_benchmark_preset(preset_name)
    ctx = resolve_server_context(paths, server_name)
    transport = _current_runtime_transport(paths, server_name)
    output_path = f"/tmp/{preset.name}.out"
    command = build_benchmark_command(
        preset,
        workspace_root=ctx.runtime.workspace_root,
        visible_devices=preset.visible_devices,
        output_path=output_path,
    )
    result = run_runtime_command(ctx, transport, command)
    if result.returncode != 0:
        print((result.stderr or result.stdout).strip())
        return 1

    marker_block = _extract_remote_marker_block(paths, server_name, output_path)
    update_state(
        paths,
        benchmark_runs={
            preset.name: {
                "server_name": server_name,
                "output_path": output_path,
                "marker_block": marker_block,
            }
        },
    )
    print(marker_block)
    return 0
