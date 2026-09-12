"""Kimi's native SessionSetup adapter creates directories only for its project."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from client_setup_fixtures import selected_runtime

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("kimi_setup", ROOT / ".agents/scripts/vaws_kimi_session_setup.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

config_spec = importlib.util.spec_from_file_location("kimi_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
client_setup = importlib.util.module_from_spec(config_spec)
with patch("vaws_venv.ensure_workspace_interpreter"):
    config_spec.loader.exec_module(client_setup)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(path):
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / ".gitignore").write_text(".vaws-local/\n")
    (path / "README.md").write_text("native session fixture\n")
    git(path, "add", ".")
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    return path


def test_creates_linked_worktrees_for_native_ids_and_keeps_existing_edits(tmp_path, monkeypatch):
    project = repo(tmp_path / "project 用户")
    monkeypatch.setattr(adapter, "prepare_worktree", lambda *_: {"status": "ready"})
    first = Path(adapter.setup(project, project, {"session_id": "session-first"})["hookSpecificOutput"]["cwd"])
    second = Path(adapter.setup(project, project, {"session_id": "session-second"})["hookSpecificOutput"]["cwd"])
    assert first != second != project
    assert git(first, "rev-parse", "HEAD") == git(project, "rev-parse", "HEAD")
    assert adapter.scoped_source(project, first) == first
    (first / "task.txt").write_text("unfinished work")
    repeated = adapter.setup(project, project, {"session_id": "session-first"})
    assert Path(repeated["hookSpecificOutput"]["cwd"]) == first
    assert (first / "task.txt").read_text() == "unfinished work"
    assert git(project, "status", "--porcelain") == ""


def test_unrelated_nested_repository_does_not_inherit_setup(tmp_path):
    project = repo(tmp_path / "project")
    nested = repo(project / "unrelated")
    assert adapter.scoped_source(project, nested) is None
    assert adapter.scoped_source(project, project) == project


def test_resume_routes_existing_receipt_without_preparing(tmp_path, monkeypatch):
    project = repo(tmp_path / "project")
    calls = []
    monkeypatch.setattr(adapter, "saved_ready", lambda _: {"python": sys.executable, "receipt": "old-receipt"})
    monkeypatch.setattr(adapter.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or type("Result", (), {"returncode": 0})())
    payload = {"hook_event_name": "SessionStart", "source": "resume", "session_id": "known-session", "cwd": str(project)}
    assert adapter.forward(project, payload) == 0
    assert calls[0][1]["env"][adapter.PIN_ENV] == "old-receipt"
    assert json.loads(calls[0][1]["input"]) == payload
    assert "--environment-receipt" in calls[0][0]


def test_native_extension_is_opt_in_and_repair_preserves_each_project_choice(tmp_path, monkeypatch):
    selected_runtime(monkeypatch, client_setup, tmp_path)
    project = repo(tmp_path / "extended")
    other = repo(tmp_path / "official")
    config = tmp_path / "config.toml"
    config.write_text('[provider]\nname = "kept"\n')
    ordinary = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)
    assert 'event = "SessionSetup"' not in ordinary["files"][config]
    extended = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True,
                                       kimi_session_setup=True)
    text = extended["files"][config]
    hooks = client_setup.tomllib.loads(text)["hooks"]
    assert hooks[0]["event"] == "SessionSetup"
    assert hooks[0]["timeout"] <= 600
    assert all("vaws_kimi_session_setup.py" in hook["command"] for hook in hooks)
    config.write_text(text)
    repaired = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)
    assert repaired["files"][config] == text
    separate = client_setup.build_plan("kimi", other, kimi_config=config, task_only=True)
    separate_hooks = client_setup.tomllib.loads(separate["files"][config])["hooks"]
    assert sum(hook["event"] == "SessionSetup" for hook in separate_hooks) == 1
    assert "vaws_session.py" in separate_hooks[-1]["command"]
    assert client_setup.tomllib.loads(separate["files"][config])["provider"] == {"name": "kept"}
