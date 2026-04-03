from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.atomic.serving_describe_session import describe_service
from tools.atomic.serving_launch_service import launch_service
from tools.atomic.serving_list_sessions import list_services
from tools.atomic.serving_probe_readiness import probe_service
from tools.atomic.serving_stop_service import stop_service
from tools.lib.config import RepoPaths


def test_launch_service_returns_structured_payload(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.serving_launch_service.launch_service_session",
        lambda *_args, **_kwargs: {
            "service_id": "svc-123",
            "server_name": "lab-a",
            "server_root_url": "http://10.0.0.12:8000",
            "lifecycle": "explicit-serving",
            "manifest_path": "/vllm-workspace/artifacts/services/svc-123.json",
        },
    )

    result = launch_service(
        RepoPaths(root=vaws_repo),
        server_name="lab-a",
        preset_name="qwen3_5_35b_tp4",
        weights_path="/home/weights/Qwen3.5-35B-A3B",
        api_key_env=None,
    )

    assert result["status"] == "ready"
    assert result["payload"]["service_id"] == "svc-123"
    assert result["payload"]["manifest_path"] == "/vllm-workspace/artifacts/services/svc-123.json"
    assert result["side_effects"] == ["service process launched", "service session recorded"]


def test_probe_service_uses_existing_service_id(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.serving_probe_readiness.probe_service_readiness",
        lambda *_args, **_kwargs: {
            "service_id": "svc-123",
            "server_name": "lab-a",
            "health_status": "ready",
            "server_root_url": "http://10.0.0.12:8000",
        },
    )

    result = probe_service(RepoPaths(root=vaws_repo), "svc-123", timeout_s=1.0)

    assert result["status"] == "ready"
    assert result["payload"]["service_id"] == "svc-123"
    assert result["payload"]["health_status"] == "ready"


def test_describe_and_list_services_use_session_payloads(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.serving_describe_session.describe_service_session",
        lambda *_args, **_kwargs: {"service_id": "svc-123", "server_name": "lab-a"},
    )
    monkeypatch.setattr(
        "tools.atomic.serving_list_sessions.list_service_sessions",
        lambda *_args, **_kwargs: [
            {"service_id": "svc-123", "server_name": "lab-a"},
            {"service_id": "svc-456", "server_name": "lab-b"},
        ],
    )

    described = describe_service(RepoPaths(root=vaws_repo), "svc-123")
    listed = list_services(RepoPaths(root=vaws_repo))

    assert described["payload"]["service_id"] == "svc-123"
    assert [item["service_id"] for item in listed["payload"]["services"]] == ["svc-123", "svc-456"]


def test_stop_service_returns_structured_cleanup_failure(vaws_repo, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("container busy")

    monkeypatch.setattr("tools.atomic.serving_stop_service.stop_service_session", fail)

    result = stop_service(RepoPaths(root=vaws_repo), "svc-123")

    assert result["status"] == "cleanup_failed"
    assert result["reason"] == "container busy"
    assert result["side_effects"] == []
