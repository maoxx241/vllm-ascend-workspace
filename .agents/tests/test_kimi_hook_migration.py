"""Only generated callbacks from the same Git family are superseded."""
import hashlib
import json
import shlex
import tomllib

from test_kimi_native_setup import client_setup, git, repo
from client_setup_fixtures import selected_runtime
import vaws_kimi_config
from vaws_kimi_config import migrate_kimi_hooks


def hook(command, event="SessionStart"):
    return f'[[hooks]]\nevent = "{event}"\ncommand = {json.dumps(command)}\ntimeout = 12\n'


def block(project, body):
    key = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    return f"# BEGIN VAWS session-{key}\n{body}# END VAWS session-{key}\n"


def test_removes_only_same_family_generated_hooks_and_preserves_custom_blocks(tmp_path):
    project = repo(tmp_path / "project 用户")
    linked = tmp_path / "old tree"
    git(project, "worktree", "add", "--detach", str(linked), "HEAD")
    other = repo(tmp_path / "independent")
    nested = project / "old-config"
    nested.mkdir()
    direct = lambda target: shlex.join(["python", str(project / ".agents/hooks/vaws_session.py"),
                                       "--client", "kimi", "--project", str(target)])
    adapter = lambda target: shlex.join(["uv", "run", "--no-project", "python",
                                        str(project / ".agents/scripts/vaws_kimi_session_setup.py"), "--project", str(target)])
    current = block(project, hook(adapter(project), "SessionSetup"))
    unrelated = block(other, hook(direct(other)))
    custom = block(nested, hook("my-custom-hook") + hook(direct(nested)))
    original = ("# keep native settings\n[thinking]\nenabled = true\n\n"
                + hook(direct(nested)) + block(linked, hook(adapter(linked)))
                + unrelated + custom + hook("another-custom-hook") + current)
    migrated = migrate_kimi_hooks(original, project, project, parse_command=shlex.split)
    assert block(linked, hook(adapter(linked))) not in migrated
    assert unrelated in migrated and custom in migrated and current in migrated
    commands = [entry["command"] for entry in tomllib.loads(migrated)["hooks"]]
    assert commands.count(direct(nested)) == 1
    assert "another-custom-hook" in commands
    assert tomllib.loads(migrated)["thinking"] == {"enabled": True}
    assert migrate_kimi_hooks(migrated, project, project, parse_command=shlex.split) == migrated


def test_preserves_custom_legacy_arguments_or_matcher(tmp_path):
    project = repo(tmp_path / "project")
    command = shlex.join(["python", str(project / ".agents/hooks/vaws_session.py"),
                          "--client", "kimi", "--project", str(project)])
    original = hook(command + " --custom preserved") + hook(command) + 'matcher = "Bash"\n'
    assert migrate_kimi_hooks(original, project, project, parse_command=shlex.split) == original


def test_repair_after_native_writer_removed_markers_keeps_one_callback(tmp_path, monkeypatch):
    receipt = selected_runtime(monkeypatch, client_setup, tmp_path)
    monkeypatch.setattr(vaws_kimi_config, "managed_receipt", lambda _: receipt)
    project = repo(tmp_path / "project")
    config = tmp_path / "config.toml"
    initial = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True, kimi_session_setup=True)
    original = initial["files"][config]
    unmarked = "\n".join(line for line in original.splitlines() if not line.startswith("#")) + "\n"
    old = shlex.join(["python", str(client_setup.ROOT / ".agents/hooks/vaws_session.py"),
                      "--client", "kimi", "--project", str(project), "--environment-receipt", "/old/receipt.json"])
    config.write_text(unmarked + hook(old))
    repaired = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)["files"][config]
    hooks = tomllib.loads(repaired)["hooks"]
    events = [entry["event"] for entry in hooks]
    assert events.count("SessionSetup") == 1
    assert len(events) == len(set(events)) == len(tomllib.loads(original)["hooks"])
    expected = ["uv", "run", "--no-project", "python",
                str(client_setup.ROOT / ".agents/scripts/vaws_kimi_session_setup.py"), "--project", str(project)]
    assert all(client_setup.hook_argv(entry["command"]) == expected for entry in hooks)
    config.write_text(repaired)
    assert client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)["files"][config] == repaired
