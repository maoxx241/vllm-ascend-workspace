from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.benchmark_execution import describe_benchmark_preset, describe_benchmark_run, run_benchmark_probe
from tools.lib.benchmark_run_artifacts import write_benchmark_run_artifact
from tools.lib.config import RepoPaths
from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext


def _ctx() -> TargetContext:
    return TargetContext(
        name="lab-a",
        host=HostSpec(
            name="lab-a",
            host="10.0.0.12",
            port=22,
            login_user="root",
            auth_group="default-server-auth",
            ssh_auth_ref="default-server-auth",
        ),
        credential=CredentialGroup(mode="ssh-key", username="root", key_path="/tmp/id_rsa"),
        runtime=RuntimeSpec(
            image_ref="registry.example.com/ascend/vllm-ascend:test",
            container_name="vaws-owner",
            ssh_port=63269,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/lab-a/workspace",
            docker_run_args=[],
        ),
    )


def test_describe_benchmark_preset_exposes_serving_linkage():
    payload = describe_benchmark_preset("qwen3_5_35b_tp4_perf")

    assert payload["preset_name"] == "qwen3_5_35b_tp4_perf"
    assert payload["serving_preset"] == "qwen3_5_35b_tp4"
    assert payload["probe_runner"] == "vllm-bench-serve"


def test_benchmark_execution_module_does_not_import_capability_state():
    source = (ROOT / "tools/lib/benchmark_execution.py").read_text(encoding="utf-8")
    assert "capability_state" not in source


def test_describe_benchmark_run_returns_latest_when_run_id_missing(tmp_path):
    paths = RepoPaths(root=tmp_path)
    write_benchmark_run_artifact(paths, "run-001", {"finished_at": 1, "summary": "old"})
    write_benchmark_run_artifact(paths, "run-002", {"finished_at": 2, "summary": "new"})

    payload = describe_benchmark_run(paths)

    assert payload["run_id"] == "run-002"
    assert payload["summary"] == "new"


def test_run_benchmark_probe_writes_local_run_artifact_for_existing_service(monkeypatch, tmp_path):
    paths = RepoPaths(root=tmp_path)
    monkeypatch.setattr(
        "tools.lib.benchmark_execution.load_service_session",
        lambda _paths, _service_id: {
            "service_id": "svc-123",
            "server_name": "lab-a",
            "server_root_url": "http://10.0.0.12:8000",
            "primary_served_model_name": "Qwen3.5-35B-A3B",
            "code_fingerprint": {"workspace": "ws", "vllm": "vv", "vllm_ascend": "va"},
        },
    )
    monkeypatch.setattr(
        "tools.lib.benchmark_execution.current_code_fingerprint",
        lambda _paths, _server_name: {"workspace": "ws", "vllm": "vv", "vllm_ascend": "va"},
    )
    monkeypatch.setattr("tools.lib.benchmark_execution.resolve_server_context", lambda _paths, _server_name: _ctx())
    monkeypatch.setattr(
        "tools.lib.benchmark_execution.resolve_available_runtime_transport",
        lambda _ctx: "docker-exec",
    )

    def fake_run(_ctx, transport, script):
        if script.startswith("cat "):
            return subprocess.CompletedProcess(["docker"], 0, '{"throughput": 1}', "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr("tools.lib.benchmark_execution.run_container_command", fake_run)
    monkeypatch.setattr("tools.lib.benchmark_execution.time.time", lambda: 1710000000)

    class _UUID:
        hex = "feedfacecafebeef"

    monkeypatch.setattr("tools.lib.benchmark_execution.uuid.uuid4", lambda: _UUID())

    result = run_benchmark_probe(
        paths,
        server_name="lab-a",
        preset_name="qwen3_5_35b_tp4_perf",
        service_id="svc-123",
    )

    assert result["run_id"] == "qwen3_5_35b_tp4_perf-feedface"
    assert result["service_id"] == "svc-123"
    assert result["result_path"] == "/tmp/svc-123-qwen3_5_35b_tp4_perf.json"
    assert result["artifact_path"] == str(paths.local_benchmark_runs_dir / "qwen3_5_35b_tp4_perf-feedface.json")
    assert Path(result["artifact_path"]).exists()
    assert describe_benchmark_run(paths, result["run_id"])["summary"] == '{"throughput": 1}'


def test_run_benchmark_probe_rejects_non_reusable_service(monkeypatch, tmp_path):
    paths = RepoPaths(root=tmp_path)
    monkeypatch.setattr(
        "tools.lib.benchmark_execution.load_service_session",
        lambda _paths, _service_id: {
            "service_id": "svc-123",
            "server_name": "lab-a",
            "code_fingerprint": {"workspace": "old"},
        },
    )
    monkeypatch.setattr(
        "tools.lib.benchmark_execution.current_code_fingerprint",
        lambda _paths, _server_name: {"workspace": "new"},
    )

    with pytest.raises(RuntimeError, match="does not match current workspace state"):
        run_benchmark_probe(
            paths,
            server_name="lab-a",
            preset_name="qwen3_5_35b_tp4_perf",
            service_id="svc-123",
        )
