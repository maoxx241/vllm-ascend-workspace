import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.benchmark import build_benchmark_command, get_benchmark_preset
from tools.vaws import main as vaws_main


def test_qwen3_35b_tp4_preset_resolves_asset_paths():
    preset = get_benchmark_preset("qwen3-35b-tp4")

    assert preset.model_path == "/home/weights/Qwen3.5-35B-A3B"
    assert preset.tensor_parallel_size == 4
    assert preset.script_path.name == "qwen35_gdn_chunk_perf.py"
    assert preset.runbook_path.name == "RUNBOOK.md"


def test_build_benchmark_command_uses_remote_log_redirection_and_markers():
    preset = get_benchmark_preset("qwen3-35b-tp4")
    command = build_benchmark_command(
        preset,
        workspace_root="/vllm-workspace",
        visible_devices="0,1,2,3",
        output_path="/tmp/qwen35_35b_tp4.out",
    )

    assert "ASCEND_RT_VISIBLE_DEVICES=0,1,2,3" in command
    assert "/tmp/qwen35_35b_tp4.out" in command
    assert "JSON_RESULTS_BEGIN" in preset.result_markers
    assert "MARKDOWN_ROWS_BEGIN" in preset.result_markers


def test_benchmark_cli_run_delegates_to_backend(monkeypatch, vaws_repo):
    calls = {}
    monkeypatch.chdir(vaws_repo)

    def fake_run(paths, server_name, preset_name):
        calls["root"] = str(paths.root)
        calls["server_name"] = server_name
        calls["preset_name"] = preset_name
        return 0

    monkeypatch.setattr("tools.vaws.run_benchmark_preset", fake_run)

    result = vaws_main(
        [
            "benchmark",
            "run",
            "--server-name",
            "lab-a",
            "--preset",
            "qwen3-35b-tp4",
        ]
    )

    assert result == 0
    assert calls == {
        "root": str(vaws_repo),
        "server_name": "lab-a",
        "preset_name": "qwen3-35b-tp4",
    }
