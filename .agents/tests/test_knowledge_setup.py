"""Local preparation and public contribution remain separate setup choices."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("workspace_knowledge_setup", ROOT / ".agents/scripts/knowledge_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.mark.parametrize("publishing", [{}, {"enabled": False},
    {"enabled": True, "repository": "example/references"},
    {"enabled": True, "repository": "example/references", "fork": "example/fork"}])
def test_default_setup_prepares_locally_without_changing_publishing(tmp_path, monkeypatch, capsys, publishing):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    path = tmp_path / ".vaws-local/knowledge/service.json"
    path.parent.mkdir(parents=True)
    original = json.dumps({"publishing": publishing, "custom": "preserve"}, indent=2)
    path.write_text(original, encoding="utf-8")
    calls = []
    monkeypatch.setattr(setup, "prepare_knowledge", lambda root: calls.append(root) or {"status": "ready", "ready": True})
    monkeypatch.setattr(setup, "run_knowledge_cli", lambda *args: pytest.fail("default setup must not configure a public fork"))
    assert setup.main([]) == 0
    assert calls == [tmp_path]
    assert path.read_text(encoding="utf-8") == original
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_first_setup_delegates_local_configuration_to_package(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(setup, "prepare_knowledge", lambda root: calls.append(root) or {"status": "ready", "ready": True})
    monkeypatch.setattr(setup, "run_knowledge_cli", lambda *args: pytest.fail("fresh setup must not create a fork"))
    assert setup.main([]) == 0
    assert calls == [tmp_path]


@pytest.mark.parametrize("arguments,publishing,read_only", [
    (["--read-only"], {"enabled": True, "fork": "example/fork"}, True),
    (["--repository", "example/new"], {"enabled": True}, True),
    (["--repository", "example/new"], {"enabled": False, "fork": "example/fork"}, True),
    (["--repository", "example/new"], {"enabled": True, "fork": "example/fork"}, False),
    (["--contribute"], {"enabled": False}, False),
])
def test_explicit_configuration_preserves_mode_unless_requested(tmp_path, monkeypatch, arguments, publishing, read_only):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    path = tmp_path / ".vaws-local/knowledge/service.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"publishing": publishing, "shared_sync": {"repository": "example/references"}}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(setup, "prepare_knowledge", lambda root: {"status": "ready", "ready": True})
    monkeypatch.setattr(setup, "run_knowledge_cli", lambda root, command: calls.append((root, command)) or (0, {"status": "configured"}))
    assert setup.main(arguments) == 0
    root, command = calls[0]
    assert root == tmp_path
    assert command[:4] == ["publishing", "configure", "--config", str(path)]
    assert command[command.index("--repository") + 1] == ("example/new" if "--repository" in arguments else "example/references")
    assert ("--read-only" in command) is read_only


def test_pending_preparation_is_reported_without_configuring_contribution(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    monkeypatch.setattr(setup, "prepare_knowledge", lambda root: {"status": "pending", "ready": False, "reason": "offline"})
    monkeypatch.setattr(setup, "run_knowledge_cli", lambda *args: pytest.fail("pending setup must not enable contribution"))
    assert setup.main([]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "offline"


def test_explicit_read_only_is_configured_before_any_maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    operations = []
    monkeypatch.setattr(setup, "run_knowledge_cli", lambda root, command: operations.append(command) or (0, {"status": "configured"}))
    monkeypatch.setattr(setup, "prepare_knowledge", lambda root: operations.append("prepare") or {"status": "ready", "ready": True})
    assert setup.main(["--read-only"]) == 0
    assert "--read-only" in operations[0]
    assert operations[1] == "prepare"
