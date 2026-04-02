import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.benchmark import run_benchmark
from tools.lib.config import RepoPaths
from tools.vaws import main as vaws_main


def test_run_benchmark_starts_and_stops_temporary_service(monkeypatch, tmp_path):
    paths = RepoPaths(root=tmp_path)
    started = {}
    stopped = []

    monkeypatch.setattr(
        "tools.lib.benchmark.start_temporary_service",
        lambda *args, **kwargs: started.update(kwargs)
        or {"service_id": "svc-temp", "server_name": "box-a"},
    )
    monkeypatch.setattr(
        "tools.lib.benchmark.stop_service_by_id",
        lambda paths, service_id: stopped.append(service_id),
    )
    monkeypatch.setattr(
        "tools.lib.benchmark.run_benchmark_probe",
        lambda *args, **kwargs: {"summary": "ok", "result_path": "/tmp/result.json"},
    )
    monkeypatch.setattr("tools.lib.benchmark.record_benchmark_run", lambda *args, **kwargs: None)

    rc = run_benchmark(
        paths,
        server_name="box-a",
        preset_name="qwen3_5_35b_tp4_perf",
        weights_path="/home/weights/Qwen3.5-35B-A3B",
        service_id=None,
    )

    assert rc == 0
    assert started["lifecycle"] == "benchmark-temporary"
    assert stopped == ["svc-temp"]


def test_run_benchmark_rejects_mismatched_explicit_service(monkeypatch, tmp_path):
    paths = RepoPaths(root=tmp_path)
    monkeypatch.setattr(
        "tools.lib.benchmark.load_service_session",
        lambda paths, service_id: {
            "service_id": service_id,
            "server_name": "box-a",
            "code_fingerprint": {"workspace": "old"},
        },
    )
    monkeypatch.setattr(
        "tools.lib.benchmark.current_code_fingerprint",
        lambda paths, server_name: {"workspace": "new"},
    )

    rc = run_benchmark(
        paths,
        server_name="box-a",
        preset_name="qwen3_5_35b_tp4_perf",
        weights_path=None,
        service_id="svc-explicit",
    )

    assert rc == 1


def test_benchmark_cli_run_delegates_to_backend(monkeypatch, vaws_repo):
    calls = {}
    monkeypatch.chdir(vaws_repo)

    def fake_run(paths, server_name, preset_name, weights_path, service_id):
        calls["root"] = str(paths.root)
        calls["server_name"] = server_name
        calls["preset_name"] = preset_name
        calls["weights_path"] = weights_path
        calls["service_id"] = service_id
        return 0

    monkeypatch.setattr("tools.vaws.run_benchmark", fake_run)

    result = vaws_main(
        [
            "benchmark",
            "run",
            "--server-name",
            "lab-a",
            "--preset",
            "qwen3_5_35b_tp4_perf",
            "--weights-path",
            "/home/weights/Qwen3.5-35B-A3B",
        ]
    )

    assert result == 0
    assert calls == {
        "root": str(vaws_repo),
        "server_name": "lab-a",
        "preset_name": "qwen3_5_35b_tp4_perf",
        "weights_path": "/home/weights/Qwen3.5-35B-A3B",
        "service_id": None,
    }
