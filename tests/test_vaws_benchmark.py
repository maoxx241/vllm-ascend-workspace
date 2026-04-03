import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.vaws_benchmark import run_benchmark
from tools.vaws import main as vaws_main


def test_run_benchmark_requires_explicit_service_id(monkeypatch, tmp_path, capsys):
    paths = RepoPaths(root=tmp_path)

    rc = run_benchmark(
        paths,
        server_name="box-a",
        preset_name="qwen3_5_35b_tp4_perf",
        service_id=None,
    )

    assert rc == 1
    assert "service-id" in capsys.readouterr().out.lower()


def test_run_benchmark_rejects_mismatched_explicit_service(monkeypatch, tmp_path):
    paths = RepoPaths(root=tmp_path)
    monkeypatch.setattr(
        "tools.lib.vaws_benchmark.load_service_session",
        lambda paths, service_id: {
            "service_id": service_id,
            "server_name": "box-a",
            "code_fingerprint": {"workspace": "old"},
        },
    )
    monkeypatch.setattr(
        "tools.lib.vaws_benchmark.service_is_reusable",
        lambda paths, server_name, service: False,
    )

    rc = run_benchmark(
        paths,
        server_name="box-a",
        preset_name="qwen3_5_35b_tp4_perf",
        service_id="svc-explicit",
    )

    assert rc == 1


def test_benchmark_cli_run_delegates_to_backend(monkeypatch, vaws_repo):
    calls = {}
    monkeypatch.chdir(vaws_repo)

    def fake_run(paths, args):
        calls["root"] = str(paths.root)
        calls["benchmark_command"] = args.benchmark_command
        calls["server_name"] = args.server_name
        calls["preset_name"] = args.preset
        calls["service_id"] = args.service_id
        return 0

    monkeypatch.setattr("tools.vaws.vaws_benchmark", SimpleNamespace(run=fake_run), raising=False)

    result = vaws_main(
        [
            "benchmark",
            "run",
            "--server-name",
            "lab-a",
            "--preset",
            "qwen3_5_35b_tp4_perf",
            "--service-id",
            "svc-123",
        ]
    )

    assert result == 0
    assert calls == {
        "root": str(vaws_repo),
        "benchmark_command": "run",
        "server_name": "lab-a",
        "preset_name": "qwen3_5_35b_tp4_perf",
        "service_id": "svc-123",
    }
