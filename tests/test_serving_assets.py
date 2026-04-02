import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.benchmark_assets import load_benchmark_preset  # type: ignore[attr-defined]
from tools.lib.serving_assets import (  # type: ignore[attr-defined]
    load_serving_preset,
    materialize_serving_config,
)


def test_materialize_serving_config_shallow_overrides_additional_config():
    preset = load_serving_preset("qwen3_5_35b_tp4")

    config = materialize_serving_config(
        preset,
        weights_input="/home/weights/Qwen3.5-35B-A3B",
    )

    assert config.runner_kind == "generate"
    assert config.device_binding == "0,1,2,3"
    assert config.additional_config["ascend_compilation_config"] == {
        "enable_npugraph_ex": True,
    }


def test_materialize_serving_config_preset_wins_on_additional_config_conflict():
    preset = load_serving_preset("qwen3_5_35b_tp4")

    config = materialize_serving_config(
        preset,
        weights_input="/home/weights/Qwen3.5-35B-A3B",
    )

    assert config.additional_config["enable_cpu_binding"] is False


def test_load_benchmark_preset_references_serving_preset():
    preset = load_benchmark_preset("qwen3_5_35b_tp4_perf")

    assert preset.serving_preset == "qwen3_5_35b_tp4"
    assert preset.probe_runner == "vllm-bench-serve"
    assert preset.request_family == "openai-chat"
