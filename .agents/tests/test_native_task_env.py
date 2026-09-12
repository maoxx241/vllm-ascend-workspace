"""User MCP and native hooks keep one configured registry across worktrees."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
sys.path.insert(0, str(ROOT / ".agents/scripts"))
from vaws_native_task_env import task_env, user_task_env
import vaws_kimi_session_setup as kimi
import vaws_native_mcp as provider
import vaws_claude_entry

spec = importlib.util.spec_from_file_location("registry_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
setup = importlib.util.module_from_spec(spec)
with patch("vaws_venv.ensure_workspace_interpreter"):
    spec.loader.exec_module(setup)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    def git(root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    source = tmp_path / "source 用户"
    source.mkdir()
    git(source, "init")
    marker = source / ".agents/lib/vaws_environment.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("# fixture")
    git(source, "add", ".")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    target = tmp_path / "new worktree"
    git(source, "worktree", "add", "--detach", str(target), "HEAD")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("KIMI_CODE_HOME", str(home / ".kimi-code"))
    settings = {"VAWS_AGENT_SESSIONS_DIR": str(tmp_path / "custom registry"),
                "VAWS_COORDINATOR_STATE_DIR": str(tmp_path / "custom state"),
                "VAWS_GITHUB_IDENTITY_FILE": str(tmp_path / "identity.json")}
    entry = {"command": sys.executable, "args": [str(source / ".agents/scripts/vaws_native_mcp.py"), "task"],
             "env": {**settings, "VAWS_ENV_RECEIPT": "stale-pin", "VAWS_CONTEXT_FILE": "not-a-setting"}}
    for dirname in (".cursor", ".kimi-code"):
        path = home / dirname / "mcp.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"mcpServers": {"vaws-task": entry}}))
    return source, target, home, settings, entry


@pytest.mark.parametrize("client", ["cursor", "kimi"])
def test_worktree_hook_reads_the_installed_user_provider_registry(configured, client):
    source, target, home, settings, _ = configured
    temporary = target / ".vaws-local/kimi-hooks.toml" if client == "kimi" else None
    assert user_task_env(client, target, kimi_config=temporary) == settings
    assert setup.existing_task_env(client, target, kimi_config=temporary) == settings
    with patch.object(setup, "managed_python", return_value=sys.executable), \
         patch.object(setup, "managed_receipt", return_value={"receipt": "selected-target-pin"}):
        arguments = setup.hook_argv(setup.hook_command(client, target, settings))
    assert arguments[arguments.index("--agent-sessions-dir") + 1] == settings["VAWS_AGENT_SESSIONS_DIR"]
    assert arguments[arguments.index("--coordinator-state-dir") + 1] == settings["VAWS_COORDINATOR_STATE_DIR"]
    assert arguments[arguments.index("--environment-receipt") + 1] == "selected-target-pin"


def test_kimi_forward_and_provider_use_same_custom_registry_with_target_pin(configured, monkeypatch):
    source, target, _, settings, entry = configured
    receipt = {"python": sys.executable, "receipt": "selected-target-pin"}
    monkeypatch.setattr(kimi, "saved_ready", lambda _: receipt)
    calls = []
    native_run = subprocess.run
    def run(command, **kwargs):
        if command[0] == "git":
            return native_run(command, **kwargs)
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()
    with patch.object(kimi.subprocess, "run", side_effect=run):
        assert kimi.forward(target, {"hook_event_name": "SessionStart", "session_id": "native", "cwd": str(target)}) == 0
    monkeypatch.setattr(provider, "ROOT", source)
    monkeypatch.setattr(vaws_claude_entry, "saved_ready", lambda _: receipt)
    cwd, _, environment = provider.provider_plan("task", target, entry["env"])
    assert cwd == target
    for key, value in settings.items():
        assert calls[0][1]["env"][key] == environment[key] == value
    assert calls[0][1]["env"][provider.PIN_ENV] == environment[provider.PIN_ENV] == receipt["receipt"]


@pytest.mark.parametrize("client", ["cursor", "kimi"])
def test_project_provider_keeps_its_explicit_registry(configured, client):
    _, target, _, _, _ = configured
    path = target / (".cursor" if client == "cursor" else ".kimi-code") / "mcp.json"
    path.parent.mkdir()
    local = {"VAWS_AGENT_SESSIONS_DIR": "explicit-project-registry"}
    path.write_text(json.dumps({"mcpServers": {"vaws-task": {"command": "custom", "env": local}}}))
    assert setup.existing_task_env(client, target) == task_env(client, target) == local


def test_unrelated_project_does_not_read_another_workspace_registry(configured, tmp_path):
    assert user_task_env("cursor", tmp_path) == {}
    assert user_task_env("kimi", tmp_path) == {}
