from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.remote_types import CredentialGroup, HostSpec, RuntimeSpec, TargetContext
from tools.lib.serving_assets import MaterializedServingConfig
from tools.lib.serving_lifecycle import launch_service_session, probe_service_readiness, stop_service_session


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


def _config() -> MaterializedServingConfig:
    return MaterializedServingConfig(
        preset_name="qwen3_5_35b_tp4",
        topology="single-node-replica",
        model_profile="qwen3_5",
        runner_kind="generate",
        served_model_name="Qwen3.5-35B-A3B",
        weights_input="/home/weights/Qwen3.5-35B-A3B",
        device_binding="0,1,2,3",
        port=8000,
        additional_config={"max_num_batched_tokens": 1024},
        serve_args={"tensor_parallel_size": 4},
    )


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_launch_service_session_writes_remote_manifest_before_returning(monkeypatch, vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    captured: dict[str, object] = {}
    monkeypatch.setattr("tools.lib.serving_lifecycle.load_serving_preset", lambda name: object())
    monkeypatch.setattr("tools.lib.serving_lifecycle.materialize_serving_config", lambda preset, weights_input: _config())
    monkeypatch.setattr("tools.lib.serving_lifecycle.resolve_server_context", lambda _paths, _server_name: _ctx())
    monkeypatch.setattr(
        "tools.lib.serving_lifecycle.resolve_available_runtime_transport",
        lambda _ctx: "docker-exec",
    )
    monkeypatch.setattr(
        "tools.lib.serving_lifecycle.run_detached_container_command",
        lambda _ctx, transport, command, *, log_path, pid_path: subprocess.CompletedProcess(
            ["remote"],
            0,
            "4321\n",
            "",
        ),
    )
    monkeypatch.setattr(
        "tools.lib.serving_lifecycle.upsert_service_session_record",
        lambda _paths, service: captured.setdefault("service", dict(service)) or dict(service),
    )

    service = launch_service_session(
        paths,
        server_name="lab-a",
        preset_name="qwen3_5_35b_tp4",
        weights_path="/home/weights/Qwen3.5-35B-A3B",
        api_key_env=None,
        lifecycle="explicit-serving",
    )

    stored = captured["service"]
    assert stored["health_status"] == "starting"
    assert stored["runtime_fingerprint"] == {"transport": "docker-exec"}
    assert stored["process"]["pid"] == "4321"
    assert stored["manifest_path"].endswith(".json")
    assert service["manifest_path"] == stored["manifest_path"]


def test_probe_service_readiness_updates_remote_manifest(monkeypatch, tmp_path):
    paths = RepoPaths(root=tmp_path)
    updated: dict[str, object] = {}
    monkeypatch.setattr(
        "tools.lib.serving_lifecycle.load_service_session",
        lambda _paths, _service_id: {
            "service_id": "svc-123",
            "server_name": "lab-a",
            "server_root_url": "http://10.0.0.12:8000",
            "primary_served_model_name": "Qwen3.5-35B-A3B",
            "health_status": "starting",
            "openai_api_auth_ref": None,
            "manifest_path": "/vllm-workspace/artifacts/services/svc-123.json",
        },
    )
    monkeypatch.setattr("tools.lib.serving_lifecycle.urllib.request.urlopen", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(
        "tools.lib.serving_lifecycle.upsert_service_session_record",
        lambda _paths, service: updated.setdefault("service", dict(service)) or dict(service),
    )

    service = probe_service_readiness(paths, "svc-123", timeout_s=0.1)

    assert service["health_status"] == "ready"
    assert updated["service"]["health_status"] == "ready"


def test_stop_service_session_uses_live_transport_and_removes_record(monkeypatch, tmp_path):
    paths = RepoPaths(root=tmp_path)
    monkeypatch.setattr(
        "tools.lib.serving_lifecycle.load_service_session",
        lambda _paths, _service_id: {
            "service_id": "svc-123",
            "server_name": "lab-a",
            "process": {"pid_path": "/tmp/svc-123.pid"},
            "manifest_path": "/vllm-workspace/artifacts/services/svc-123.json",
        },
    )
    monkeypatch.setattr("tools.lib.serving_lifecycle.resolve_server_context", lambda _paths, _server_name: _ctx())
    monkeypatch.setattr(
        "tools.lib.serving_lifecycle.resolve_available_runtime_transport",
        lambda _ctx: "docker-exec",
    )
    captured: dict[str, object] = {}
    removed: dict[str, object] = {}

    def fake_run(_ctx, transport, script):
        captured["transport"] = transport
        captured["script"] = script
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr("tools.lib.serving_lifecycle.run_container_command", fake_run)
    monkeypatch.setattr(
        "tools.lib.serving_lifecycle.remove_service_session_record",
        lambda _paths, service_id: removed.setdefault("service_id", service_id),
    )

    stop_service_session(paths, "svc-123")

    assert captured["transport"] == "docker-exec"
    assert "/tmp/svc-123.pid" in str(captured["script"])
    assert removed["service_id"] == "svc-123"
