import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.benchmark import (
    _extract_remote_marker_block,
    BenchmarkPreset,
    build_benchmark_command,
    get_benchmark_preset,
    run_benchmark_preset,
)
from tools.lib.config import RepoPaths
from tools.lib.remote import CredentialGroup, HostSpec, RuntimeSpec, TargetContext
from tools.lib.runtime import read_state, update_state
from tools.vaws import main as vaws_main
from tests.conftest import seed_overlay_files


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


def test_build_benchmark_command_shell_quotes_runtime_paths_and_arguments():
    preset = get_benchmark_preset("qwen3-35b-tp4")
    workspace_root = "/tmp/work space; echo pwned"
    visible_devices = "0,1,2,3; echo nope"
    output_path = "/tmp/results dir/output; rm -rf /tmp/nope"
    preset = BenchmarkPreset(
        name=preset.name,
        script_path=preset.script_path,
        runbook_path=preset.runbook_path,
        model_path="/models/Qwen 3.5; touch /tmp/nope",
        tensor_parallel_size=preset.tensor_parallel_size,
        visible_devices=preset.visible_devices,
        result_markers=preset.result_markers,
    )

    command = build_benchmark_command(
        preset,
        workspace_root=workspace_root,
        visible_devices=visible_devices,
        output_path=output_path,
    )

    expected_workspace = shlex.quote(f"{workspace_root}/workspace")
    expected_env = shlex.quote(
        f"{workspace_root}/workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash"
    )
    expected_script = shlex.quote(
        f"{workspace_root}/workspace/{preset.script_path.relative_to(ROOT).as_posix()}"
    )
    assert f"cd {expected_workspace}" in command
    assert f"source {expected_env}" in command
    assert (
        f"export ASCEND_RT_VISIBLE_DEVICES={shlex.quote(visible_devices)}"
        in command
    )
    assert (
        f"/usr/local/python3.11.14/bin/python3 {expected_script} --model-path "
        f"{shlex.quote(preset.model_path)} >{shlex.quote(output_path)} 2>&1"
    ) in command


def test_extract_remote_marker_block_quotes_output_path(monkeypatch, vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    seed_overlay_files(vaws_repo)
    update_state(
        paths,
        current_target="lab-a",
        runtime={"transport": "ssh"},
    )
    captured = {}

    monkeypatch.setattr(
        "tools.lib.benchmark.resolve_server_context",
        lambda _paths, _server_name: TargetContext(
            name="lab-a",
            host=HostSpec(
                name="lab-a",
                host="10.0.0.12",
                port=22,
                login_user="root",
                auth_group="default-server-auth",
                ssh_auth_ref="default-server-auth",
            ),
            credential=CredentialGroup(mode="ssh-key", username="root"),
            runtime=RuntimeSpec(
                image_ref="image",
                container_name="container",
                ssh_port=63269,
                workspace_root="/vllm-workspace",
                bootstrap_mode="host-then-container",
                host_workspace_path="/data/.vaws/workspaces/demo",
                docker_run_args=[],
            ),
        ),
    )

    def fake_run_runtime_command(ctx, transport, script):
        captured["transport"] = transport
        captured["script"] = script
        return type("Result", (), {"returncode": 0, "stdout": "rows", "stderr": ""})()

    monkeypatch.setattr("tools.lib.benchmark.run_runtime_command", fake_run_runtime_command)

    result = _extract_remote_marker_block(
        paths,
        "lab-a",
        "/tmp/results dir/output; rm -rf /tmp/nope",
    )

    assert result == "rows"
    assert captured["transport"] == "ssh"
    assert captured["script"] == (
        "sed -n '/MARKDOWN_ROWS_BEGIN/,/MARKDOWN_ROWS_END/p' "
        f"{shlex.quote('/tmp/results dir/output; rm -rf /tmp/nope')}"
    )


def test_run_benchmark_preset_merges_existing_benchmark_runs(monkeypatch, vaws_repo, capsys):
    paths = RepoPaths(root=vaws_repo)
    seed_overlay_files(vaws_repo)
    update_state(
        paths,
        benchmark_runs={
            "older-run": {
                "server_name": "lab-b",
                "output_path": "/tmp/older.out",
                "marker_block": "older rows",
            }
        },
    )

    monkeypatch.setattr(
        "tools.lib.benchmark.resolve_server_context",
        lambda _paths, _server_name: TargetContext(
            name="lab-a",
            host=HostSpec(
                name="lab-a",
                host="10.0.0.12",
                port=22,
                login_user="root",
                auth_group="default-server-auth",
                ssh_auth_ref="default-server-auth",
            ),
            credential=CredentialGroup(mode="ssh-key", username="root"),
            runtime=RuntimeSpec(
                image_ref="image",
                container_name="container",
                ssh_port=63269,
                workspace_root="/vllm-workspace",
                bootstrap_mode="host-then-container",
                host_workspace_path="/data/.vaws/workspaces/demo",
                docker_run_args=[],
            ),
        ),
    )
    monkeypatch.setattr("tools.lib.benchmark._current_runtime_transport", lambda *_args: "docker-exec")
    monkeypatch.setattr(
        "tools.lib.benchmark.run_runtime_command",
        lambda *_args: type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(
        "tools.lib.benchmark._extract_remote_marker_block",
        lambda *_args: "new rows",
    )

    result = run_benchmark_preset(paths, "lab-a", "qwen3-35b-tp4")

    assert result == 0
    assert capsys.readouterr().out.strip() == "new rows"
    benchmark_runs = read_state(paths)["benchmark_runs"]
    assert benchmark_runs["older-run"]["output_path"] == "/tmp/older.out"
    assert benchmark_runs["qwen3-35b-tp4"]["server_name"] == "lab-a"


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
