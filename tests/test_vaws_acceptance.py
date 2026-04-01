import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.acceptance import AcceptanceRequest, run_acceptance
from tools.vaws import main as vaws_main


def test_acceptance_run_calls_init_then_parity_then_benchmark(monkeypatch, vaws_repo):
    calls = []

    monkeypatch.setattr("tools.lib.acceptance.run_init", lambda paths, request: calls.append("init") or 0)
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_code_parity_for_acceptance",
        lambda paths, request: calls.append("parity") or 0,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.run_benchmark_for_acceptance",
        lambda paths, request: calls.append("benchmark") or 0,
    )

    result = run_acceptance(
        Path(vaws_repo),
        AcceptanceRequest(
            server_name="lab-a",
            server_host="10.0.0.12",
            server_user="root",
            server_password_env="VAWS_SERVER_PASSWORD",
            vllm_origin_url=None,
            vllm_ascend_origin_url=None,
            vllm_upstream_tag="0.18.0",
            vllm_ascend_upstream_branch="main",
            benchmark_preset="qwen3-35b-tp4",
        ),
    )

    assert result == 0
    assert calls == ["init", "parity", "benchmark"]


def test_acceptance_run_stops_before_benchmark_when_parity_fails(monkeypatch, vaws_repo):
    calls = []

    monkeypatch.setattr("tools.lib.acceptance.run_init", lambda paths, request: calls.append("init") or 0)
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_code_parity_for_acceptance",
        lambda paths, request: calls.append("parity") or 1,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.run_benchmark_for_acceptance",
        lambda paths, request: calls.append("benchmark") or 0,
    )

    result = run_acceptance(
        Path(vaws_repo),
        AcceptanceRequest(
            server_name="lab-a",
            server_host="10.0.0.12",
            server_user="root",
            server_password_env="VAWS_SERVER_PASSWORD",
            vllm_origin_url=None,
            vllm_ascend_origin_url=None,
            vllm_upstream_tag="0.18.0",
            vllm_ascend_upstream_branch="main",
            benchmark_preset="qwen3-35b-tp4",
        ),
    )

    assert result == 1
    assert calls == ["init", "parity"]


def test_acceptance_cli_run_delegates_to_backend(monkeypatch, vaws_repo):
    calls = {}
    monkeypatch.chdir(vaws_repo)

    def fake_run(root, request):
        calls["root"] = str(root)
        calls["request"] = request
        return 0

    monkeypatch.setattr("tools.vaws.run_acceptance", fake_run)

    result = vaws_main(
        [
            "acceptance",
            "run",
            "--server-name",
            "lab-a",
            "--server-host",
            "10.0.0.12",
            "--server-password-env",
            "VAWS_SERVER_PASSWORD",
            "--vllm-upstream-tag",
            "0.18.0",
            "--vllm-ascend-upstream-branch",
            "main",
            "--benchmark-preset",
            "qwen3-35b-tp4",
        ]
    )

    assert result == 0
    assert calls["root"] == str(vaws_repo)
    assert calls["request"].server_name == "lab-a"
    assert calls["request"].benchmark_preset == "qwen3-35b-tp4"
