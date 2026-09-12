"""Claude's creation and late provider binding use real Git directories."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_claude_config import add_claude_setup


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ".agents/scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry, creator = load("vaws_claude_entry"), load("vaws_claude_worktree")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "source 用户"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / ".gitignore").write_text(".claude/\n.vaws-local/\n.mcp.json\n")
    (root / "README.md").write_text("source unchanged\n")
    for relative in ("lib/vaws_environment.py", "scripts/vaws_claude_entry.py", "scripts/vaws_claude_worktree.py",
                     "hooks/vaws_session.py", "hooks/knowledge_summary.py"):
        path = root / ".agents" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    return root


def test_create_native_worktree_preserves_source_and_custom_configuration(repository, monkeypatch):
    settings = repository / ".claude/settings.local.json"
    settings.parent.mkdir()
    settings.write_text('{"permissions":{"deny":["Bash(rm *)"]}}')
    (repository / ".mcp.json").write_text('{"mcpServers":{"user-owned":{"command":"custom"}}}')
    original = git(repository, "rev-parse", "HEAD")
    calls = []
    monkeypatch.setattr(creator, "prepare", lambda source, target: calls.append((source, target)) or {"status": "ready"})
    target, result = creator.create_worktree({"hook_event_name": "WorktreeCreate", "cwd": str(repository), "name": "native-task"})
    assert calls == [(repository, target)]
    assert git(target, "symbolic-ref", "--short", "HEAD") == "worktree/native-task"
    assert git(repository, "rev-parse", "HEAD") == original
    assert (target / ".claude/settings.local.json").read_text() == settings.read_text()
    assert json.loads((target / ".mcp.json").read_text())["mcpServers"]["user-owned"]["command"] == "custom"
    assert json.loads((target / ".vaws-local/claude-worktree-setup.json").read_text()) == result


@pytest.mark.parametrize("name", ["../escape", "has/slash", "$(touch evil)", "", "has..dots"])
def test_invalid_name_never_creates_directory(repository, name):
    with pytest.raises(ValueError):
        creator.create_worktree({"hook_event_name": "WorktreeCreate", "cwd": str(repository), "name": name})
    assert "worktree/" not in git(repository, "branch", "--list")


def test_setup_failure_retains_owned_worktree_with_facts(repository, monkeypatch):
    def fail(source, target):
        raise RuntimeError("fixture dependency preparation failed")
    monkeypatch.setattr(creator, "prepare", fail)
    payload = {"hook_event_name": "WorktreeCreate", "cwd": str(repository), "name": "failed"}
    with pytest.raises(RuntimeError, match="retained"):
        creator.create_worktree(payload)
    target = repository / ".claude/worktrees/failed"
    assert (target / "README.md").is_file()
    assert "fixture dependency" in json.loads((target / ".vaws-local/claude-worktree-setup.json").read_text())["error"]
    with pytest.raises(ValueError, match="resume"):
        creator.create_worktree(payload)


def test_actual_cwd_selects_linked_worktree_and_never_parent_pin(repository, tmp_path, monkeypatch):
    target = tmp_path / "native target"
    git(repository, "worktree", "add", "--detach", str(target), "HEAD")
    assert entry.workspace(target, str(repository), source=repository) == target
    monkeypatch.setattr(entry, "saved_ready", lambda chosen: {"python": "/new/python", "receipt": str(chosen / "selected.json")})
    argv, environment = entry.launch_plan("task", target, [], {"VAWS_ENV_RECEIPT": "old", "VIRTUAL_ENV": "old", "CUSTOM": "retained"})
    assert argv == ["/new/python", "-m", "vaws_coordinator", "task-server"]
    assert environment["VAWS_ENV_RECEIPT"] == str(target / "selected.json")
    assert "VIRTUAL_ENV" not in environment
    assert environment["CUSTOM"] == "retained"


def test_unrelated_repository_cannot_select_provider_environment(repository, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    (other / ".agents/lib").mkdir(parents=True)
    (other / ".agents/lib/vaws_environment.py").touch()
    with pytest.raises(ValueError, match="configured workspace"):
        entry.workspace(other, source=repository)


def test_hook_uses_actual_target_and_selected_environment(repository, monkeypatch):
    monkeypatch.setattr(entry, "saved_ready", lambda target: {"python": "/new/python", "receipt": "/new/receipt"})
    argv, _ = entry.launch_plan("session", repository, ["--agent-sessions-dir", "/registry"], {})
    assert argv == ["/new/python", str(repository / ".agents/hooks/vaws_session.py"), "--client", "claude",
                    "--project", str(repository), "--environment-receipt", "/new/receipt", "--agent-sessions-dir", "/registry"]


def plan(repository, *, custom_create=False):
    hook = shlex.join([sys.executable, str(repository / ".agents/hooks/vaws_session.py"), "--client", "claude",
                       "--project", str(repository), "--agent-sessions-dir", "/registry", "--environment-receipt", "/old"])
    settings = {"permissions": {"deny": ["Bash(rm *)"]}, "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": hook}]}]}}
    if custom_create:
        settings["hooks"]["WorktreeCreate"] = [{"hooks": [{"type": "command", "command": "user-custom-create"}]}]
    servers = {"mcpServers": {"vaws-task": {"command": sys.executable, "args": ["-m", "vaws_coordinator", "task-server"],
                                           "env": {"VAWS_ENV_RECEIPT": "/old", "CUSTOM": "kept"}},
                              "user-owned": {"command": "custom", "args": ["--serve"]}}}
    return {repository / ".claude/settings.local.json": json.dumps(settings), repository / ".mcp.json": json.dumps(servers)}


def configure(files, notes, root):
    add_claude_setup(files, notes, root, root, shell_command=shlex.join, parse_command=shlex.split,
                     owned_server=lambda server, _: server.get("command") == sys.executable)


def test_configuration_is_idempotent_and_preserves_user_settings(repository):
    files, notes = plan(repository), []
    configure(files, notes, repository)
    first = dict(files)
    configure(files, notes, repository)
    assert files == first
    settings = json.loads(files[repository / ".claude/settings.local.json"])
    servers = json.loads(files[repository / ".mcp.json"])["mcpServers"]
    assert settings["permissions"] == {"deny": ["Bash(rm *)"]}
    assert len(settings["hooks"]["WorktreeCreate"]) == 1
    assert "vaws_claude_entry.py" in settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert servers["user-owned"] == {"command": "custom", "args": ["--serve"]}
    assert servers["vaws-task"]["env"] == {"CUSTOM": "kept"}


def test_existing_custom_create_hook_stays_with_owner(repository):
    files, notes = plan(repository, custom_create=True), []
    configure(files, notes, repository)
    settings = json.loads(files[repository / ".claude/settings.local.json"])
    assert settings["hooks"]["WorktreeCreate"][0]["hooks"][0]["command"] == "user-custom-create"
    assert notes[-1]["reason"] == "custom-claude-worktree-create"


def test_old_wrapper_and_regenerated_direct_hook_run_only_once(repository):
    files, notes = plan(repository), []
    configure(files, notes, repository)
    settings = json.loads(files[repository / ".claude/settings.local.json"])
    settings["hooks"]["SessionStart"] += json.loads(plan(repository)[repository / ".claude/settings.local.json"])["hooks"]["SessionStart"]
    files[repository / ".claude/settings.local.json"] = json.dumps(settings)
    configure(files, notes, repository)
    settings = json.loads(files[repository / ".claude/settings.local.json"])
    assert sum(len(group["hooks"]) for group in settings["hooks"]["SessionStart"]) == 1
