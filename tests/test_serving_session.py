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
from tools.lib.serving_session import (
    create_service_session_record,
    current_code_fingerprint,
    describe_service_session,
    list_service_sessions,
    load_service_session,
)


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


def test_serving_session_module_does_not_import_capability_state():
    source = (ROOT / "tools/lib/serving_session.py").read_text(encoding="utf-8")
    assert "capability_state" not in source


def test_create_service_session_record_captures_transport_and_manifest_path(vaws_repo):
    paths = RepoPaths(root=vaws_repo)

    record = create_service_session_record(
        paths,
        service_id="svc-123",
        server_name="lab-a",
        weights_path="/home/weights/Qwen3.5-35B-A3B",
        pid="4321",
        pid_path="/tmp/svc-123.pid",
        log_path="/tmp/svc-123.log",
        api_key_env="VLLM_API_KEY",
        config=_config(),
        ctx=_ctx(),
        transport="docker-exec",
        lifecycle="explicit-serving",
    )

    assert record["service_id"] == "svc-123"
    assert record["server_root_url"] == "http://10.0.0.12:8000"
    assert record["openai_api_auth_ref"] == "env:VLLM_API_KEY"
    assert record["runtime_fingerprint"] == {"transport": "docker-exec"}
    assert record["health_status"] == "starting"
    assert record["manifest_path"] == "/vllm-workspace/artifacts/services/svc-123.json"


def test_list_and_describe_service_sessions_use_remote_manifests(monkeypatch, tmp_path):
    paths = RepoPaths(root=tmp_path)
    monkeypatch.setattr(
        "tools.lib.serving_session._read_all_service_manifests",
        lambda _paths: [
            {
                "service_id": "svc-2",
                "server_name": "lab-b",
                "primary_served_model_name": "b-model",
                "lifecycle": "benchmark-temporary",
            },
            {
                "service_id": "svc-1",
                "server_name": "lab-a",
                "primary_served_model_name": "a-model",
                "lifecycle": "explicit-serving",
            },
        ],
    )

    listed = list_service_sessions(paths)
    described = describe_service_session(paths, "svc-1")

    assert [item["service_id"] for item in listed] == ["svc-1", "svc-2"]
    assert described["server_name"] == "lab-a"
    assert load_service_session(paths, "svc-1")["primary_served_model_name"] == "a-model"


def test_current_code_fingerprint_reads_live_repo_targets(vaws_repo):
    paths = RepoPaths(root=vaws_repo)

    fingerprint = current_code_fingerprint(paths, "lab-a")

    assert fingerprint == {
        "workspace": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=vaws_repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip(),
        "vllm": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=vaws_repo / "vllm",
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip(),
        "vllm_ascend": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=vaws_repo / "vllm-ascend",
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip(),
    }
