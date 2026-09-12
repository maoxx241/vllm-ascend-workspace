"""Installation readiness and one native knowledge owner per mounted workspace."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import vaws_knowledge_service as knowledge
import vaws_local_owner as owner

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("knowledge_dependency_setup", ROOT / ".agents/scripts/vaws_deps.py")
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)


def test_mounted_workspace_reserves_windows_owner_before_it_is_installed(monkeypatch):
    monkeypatch.setattr(owner, "os", SimpleNamespace(name="posix", environ={"WSL_DISTRO_NAME": "test"}))
    root = PurePosixPath("/mnt/d/work")
    assert owner.windows_mounted_workspace(root)
    assert owner.managed_python(root, interpreter="/linux/python", require_windows=True) == "/mnt/d/work/.vaws-local/venvs/win32/Scripts/python.exe"
    assert not owner.windows_mounted_workspace(PurePosixPath("/opt/work"))
    assert owner.managed_python(PurePosixPath("/opt/work"), interpreter="/linux/python", require_windows=True) == "/linux/python"


def test_windows_owner_receives_native_paths_and_explicit_environment(monkeypatch):
    root = PurePosixPath("/mnt/d/work")
    monkeypatch.setattr(knowledge, "windows_mounted_workspace", lambda root: True)
    monkeypatch.setattr(knowledge, "knowledge_server_env", lambda root: {
        "VAWS_KNOWLEDGE_CONFIG": "/mnt/d/work/.vaws-local/knowledge/service.json",
        "VAWS_KNOWLEDGE_PROJECT_ROOTS": "/mnt/d/work/.agents/knowledge",
        "VAWS_KNOWLEDGE_CANDIDATE_ROOT": "/mnt/d/work/.vaws-local/knowledge/candidate",
        "VAWS_KNOWLEDGE_STATE": "/mnt/d/work/.vaws-local/knowledge/instance",
        "VAWS_KNOWLEDGE_ORIGIN_REPO": "example/repo",
    })
    monkeypatch.setenv("WSLENV", "CUSTOM/w")
    environment = knowledge.knowledge_owner_env(root)
    assert environment["VAWS_KNOWLEDGE_PROJECT_ROOTS"] == r"D:\work\.agents\knowledge"
    assert environment["VAWS_KNOWLEDGE_CONFIG"] == r"D:\work\.vaws-local\knowledge\service.json"
    assert environment["VAWS_KNOWLEDGE_STATE"] == r"D:\work\.vaws-local\knowledge\instance"
    assert environment["VAWS_KNOWLEDGE_ORIGIN_REPO"] == "example/repo"
    assert "CUSTOM/w" in environment["WSLENV"]
    assert "VAWS_KNOWLEDGE_STATE/w" in environment["WSLENV"]
    assert knowledge.knowledge_owner_path(root, root) == r"D:\work"


def test_missing_owner_reports_pending_without_running_linux_backend(tmp_path, monkeypatch):
    missing = tmp_path / "win32/Scripts/python.exe"
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root: str(missing))
    monkeypatch.setattr(knowledge.subprocess, "run", lambda *args, **kwargs: pytest.fail("missing owner must not start a fallback"))
    result = knowledge.prepare_knowledge(tmp_path)
    assert result["status"] == "pending" and result["ready"] is False
    assert not (tmp_path / "win32").exists()


@pytest.mark.parametrize("code,payload,ready", [
    (0, {"status": "ready", "ready": True}, True),
    (1, {"status": "pending", "ready": False, "reason": "offline"}, False),
    (1, {"status": "ready", "ready": True}, False),
])
def test_prepare_uses_installed_cli_and_preserves_readiness(tmp_path, monkeypatch, code, payload, ready):
    calls = []
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root: sys.executable)
    monkeypatch.setattr(knowledge, "knowledge_owner_env", lambda root: {"KNOWLEDGE_OWNER": "native"})
    monkeypatch.setattr(knowledge.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, code, json.dumps(payload)))
    result = knowledge.prepare_knowledge(tmp_path)
    command, kwargs = calls[0]
    assert command == [sys.executable, "-m", "vaws_knowledge", "prepare", "--project", str(tmp_path)]
    assert kwargs["env"]["KNOWLEDGE_OWNER"] == "native"
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] is sys.stderr
    assert result["ready"] is ready
    assert result["status"] == ("ready" if ready else "pending")


def test_invalid_prepare_reply_is_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root: sys.executable)
    monkeypatch.setattr(knowledge, "knowledge_owner_env", lambda root: {})
    monkeypatch.setattr(knowledge.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "not JSON"))
    assert knowledge.prepare_knowledge(tmp_path)["status"] == "pending"


@pytest.mark.parametrize("ready", [True, False])
def test_sync_reports_dependency_success_separately_from_knowledge(tmp_path, monkeypatch, capsys, ready):
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(deps.subprocess, "run", lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0))
    prepared = []
    monkeypatch.setattr(deps, "prepare_knowledge", lambda root: prepared.append(root) or {"status": "ready" if ready else "pending", "ready": ready})
    assert deps.main(["sync", "--locked"]) == 0
    assert calls == [["uv", "sync", "--locked"]]
    assert prepared == [tmp_path]
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["returncode"] == 0
    assert payload["knowledge"]["ready"] is ready


def test_failed_dependency_install_does_not_prepare_knowledge(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    monkeypatch.setattr(deps.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1))
    monkeypatch.setattr(deps, "prepare_knowledge", lambda root: pytest.fail("failed install must not prepare knowledge"))
    assert deps.main(["sync"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "knowledge" not in payload
