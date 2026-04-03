from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.atomic.benchmark_describe_preset import describe_preset
from tools.atomic.benchmark_describe_run import describe_run
from tools.atomic.benchmark_run_probe import run_probe
from tools.lib.config import RepoPaths


def test_run_probe_requires_existing_service_id(vaws_repo):
    result = run_probe(
        RepoPaths(root=vaws_repo),
        server_name="lab-a",
        preset_name="qwen3_5_35b_tp4_perf",
        service_id=None,
    )

    assert result["status"] == "needs_input"
    assert result["reason"] == "service_id is required for benchmark.run_probe"


def test_run_probe_returns_structured_payload(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.benchmark_run_probe.run_benchmark_probe",
        lambda *_args, **_kwargs: {
            "run_id": "run-123",
            "service_id": "svc-123",
            "result_path": "/tmp/result.json",
            "artifact_path": "/tmp/run-123.json",
            "summary": '{"throughput": 1}',
        },
    )

    result = run_probe(
        RepoPaths(root=vaws_repo),
        server_name="lab-a",
        preset_name="qwen3_5_35b_tp4_perf",
        service_id="svc-123",
    )

    assert result["status"] == "ready"
    assert result["payload"]["run_id"] == "run-123"
    assert result["payload"]["service_id"] == "svc-123"
    assert result["payload"]["artifact_path"] == "/tmp/run-123.json"


def test_describe_preset_and_run_surface_payloads(vaws_repo, monkeypatch):
    monkeypatch.setattr(
        "tools.atomic.benchmark_describe_preset.describe_benchmark_preset",
        lambda _preset_name: {"preset_name": "qwen3_5_35b_tp4_perf", "serving_preset": "qwen3_5_35b_tp4"},
    )
    monkeypatch.setattr(
        "tools.atomic.benchmark_describe_run.describe_benchmark_run",
        lambda _paths, run_id=None: {"run_id": run_id or "latest", "summary": '{"throughput": 1}'},
    )

    preset = describe_preset("qwen3_5_35b_tp4_perf")
    run = describe_run(RepoPaths(root=vaws_repo), None)

    assert preset["payload"]["serving_preset"] == "qwen3_5_35b_tp4"
    assert run["payload"]["run_id"] == "latest"
