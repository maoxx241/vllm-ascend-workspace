import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.acceptance import AcceptanceRequest, ensure_remote_baseline_for_acceptance, run_acceptance
from tools.lib.config import RepoPaths
from tools.vaws import main as vaws_main


def test_acceptance_run_calls_init_then_parity_then_benchmark(monkeypatch, vaws_repo):
    calls = []

    monkeypatch.setattr("tools.lib.acceptance.run_init", lambda paths, request: calls.append("init") or 0)
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_remote_baseline_for_acceptance",
        lambda paths, request: calls.append("baseline") or 0,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.materialize_requested_targets_for_acceptance",
        lambda paths, request: calls.append("targets") or None,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_code_parity_for_acceptance",
        lambda paths, request, desired: calls.append("parity") or 0,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_runtime_environment_for_acceptance",
        lambda paths, request, desired: calls.append("env") or 0,
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
    assert calls == ["init", "baseline", "targets", "parity", "env", "benchmark"]


def test_acceptance_run_stops_before_benchmark_when_parity_fails(monkeypatch, vaws_repo):
    calls = []

    monkeypatch.setattr("tools.lib.acceptance.run_init", lambda paths, request: calls.append("init") or 0)
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_remote_baseline_for_acceptance",
        lambda paths, request: calls.append("baseline") or 0,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.materialize_requested_targets_for_acceptance",
        lambda paths, request: calls.append("targets") or None,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_code_parity_for_acceptance",
        lambda paths, request, desired: calls.append("parity") or 1,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_runtime_environment_for_acceptance",
        lambda paths, request, desired: calls.append("env") or 0,
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
    assert calls == ["init", "baseline", "targets", "parity"]


def test_ensure_remote_baseline_for_acceptance_returns_clean_error_on_runtime_failure(
    monkeypatch,
    vaws_repo,
    capsys,
):
    paths = RepoPaths(root=vaws_repo)
    request = AcceptanceRequest(
        server_name="lab-a",
        server_host="10.0.0.12",
        server_user="root",
        server_password_env="VAWS_SERVER_PASSWORD",
        vllm_origin_url=None,
        vllm_ascend_origin_url=None,
        vllm_upstream_tag="0.18.0",
        vllm_ascend_upstream_branch="main",
        benchmark_preset="qwen3-35b-tp4",
    )

    monkeypatch.setattr("tools.lib.acceptance.resolve_repo_targets", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_code_parity",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("remote command failed")),
    )

    result = ensure_remote_baseline_for_acceptance(paths, request)

    assert result == 1
    assert capsys.readouterr().out.strip() == "remote command failed"


def test_acceptance_run_returns_clean_error_when_late_step_raises(monkeypatch, vaws_repo, capsys):
    monkeypatch.setattr("tools.lib.acceptance.run_init", lambda paths, request: 0)
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_remote_baseline_for_acceptance",
        lambda paths, request: 0,
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.materialize_requested_targets_for_acceptance",
        lambda paths, request: object(),
    )
    monkeypatch.setattr(
        "tools.lib.acceptance.ensure_code_parity_for_acceptance",
        lambda paths, request, desired: (_ for _ in ()).throw(RuntimeError("late parity failure")),
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
    assert capsys.readouterr().out.strip() == "late parity failure"


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


def test_real_acceptance_runbook_exists():
    path = (
        ROOT
        / "docs"
        / "superpowers"
        / "runbooks"
        / "2026-04-01-real-e2e-acceptance.md"
    )
    text = path.read_text(encoding="utf-8")
    assert "vaws.py acceptance run" in text
    assert "VAWS_SERVER_PASSWORD" in text
    assert "qwen3-35b-tp4" in text
    assert "173.131.1.2" not in text
