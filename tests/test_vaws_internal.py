import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import vaws
from tools.vaws import main as vaws_main


def test_public_parser_no_longer_accepts_top_level_acceptance():
    parser = vaws.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["acceptance", "run", "--server-name", "lab-a"])
    assert exc_info.value.code == 2


def test_internal_acceptance_run_rejects_attach_arguments():
    parser = vaws.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            ["internal", "acceptance", "run", "--server-name", "lab-a", "--server-host", "10.0.0.8"]
        )
    assert exc_info.value.code == 2


def test_internal_session_status_delegates_to_backend(monkeypatch, vaws_repo):
    calls = {}
    monkeypatch.chdir(vaws_repo)

    def fake_status(paths):
        calls["status"] = str(paths.root)
        return 0

    monkeypatch.setattr("tools.vaws.status_session", fake_status)

    result = vaws_main(["internal", "session", "status"])

    assert result == 0
    assert calls["status"] == str(vaws_repo)


def test_internal_acceptance_run_delegates_to_backend(monkeypatch, vaws_repo):
    calls = {}
    monkeypatch.chdir(vaws_repo)

    def fake_run(root, request):
        calls["root"] = str(root)
        calls["request"] = request
        return 0

    monkeypatch.setattr("tools.vaws.run_acceptance", fake_run)

    result = vaws_main(
        [
            "internal",
            "acceptance",
            "run",
            "--server-name",
            "lab-a",
            "--vllm-upstream-tag",
            "0.18.0",
            "--vllm-ascend-upstream-branch",
            "main",
            "--weights-path",
            "/home/weights/qwen3_5_35b",
            "--benchmark-preset",
            "qwen3_5_35b_tp4_perf",
        ]
    )

    assert result == 0
    assert calls["root"] == str(vaws_repo)
    assert calls["request"].server_name == "lab-a"
    assert calls["request"].weights_path == "/home/weights/qwen3_5_35b"
    assert calls["request"].benchmark_preset == "qwen3_5_35b_tp4_perf"
