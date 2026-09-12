"""Codex user hooks retain review definitions and execute the selected worktree."""
from __future__ import annotations

import io
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import venv
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
sys.path.insert(0, str(ROOT / ".agents/scripts"))
import vaws_codex_config as config
import vaws_codex_session as adapter
import vaws_environment as envs


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def family(tmp_path):
    source = tmp_path / "source 用户"
    source.mkdir()
    git(source, "init", "-q")
    (source / "pyproject.toml").write_text("[project]\nname='hook-fixture'\nversion='0'\n[tool.uv]\npackage=false\n")
    (source / "uv.lock").write_text("version = 1\n")
    scripts = source / ".agents/hooks"
    scripts.mkdir(parents=True)
    # Executed by a real selected interpreter. Expose only fixture facts, not
    # the inherited environment or any native account configuration.
    code = ("import json,os,sys\nfrom pathlib import Path\n"
            "print(json.dumps({'hook':Path(__file__).name, 'cwd':str(Path.cwd()),"
            "'python':sys.executable, 'payload':json.load(sys.stdin), 'argv':sys.argv[1:],"
            "'env':{k:os.environ.get(k) for k in ['VAWS_ENV_RECEIPT','VAWS_CONTEXT_FILE',"
            "'VAWS_AGENT_SESSIONS_DIR','VAWS_COORDINATOR_STATE_DIR']}}))\n")
    for name in ("vaws_session.py", "knowledge_summary.py"):
        (scripts / name).write_text(code)
    git(source, "add", ".")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    target = tmp_path / "linked worktree"
    git(source, "worktree", "add", "--detach", str(target), "HEAD")
    return source, target


def ready(root):
    _, _, document, input_id, lock_sha = envs._inputs(root)
    selection, identity = envs._selection(document)[0], envs._identity()
    key = envs._key(identity, input_id, selection)
    store = root.parent / "ready-store"
    directory = store / key
    if not directory.exists():
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(directory)
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    receipt = {"schema_version": 1, "recipe_version": envs.RECIPE_VERSION, "key": key,
               "root": str(directory), "python": str(python),
               "base_python": str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
               "python_identity": identity, "input_id": input_id, "lock_sha256": lock_sha,
               "selection": selection, "store": str(store), "receipt": str(directory / envs.READY_NAME),
               **{name: identity[name] for name in ("platform", "arch", "abi", "python_version")}}
    (directory / envs.READY_NAME).write_text(json.dumps(receipt))
    envs.select_environment(root, receipt)
    return receipt


@pytest.mark.parametrize("event,hook", [("SessionStart", "vaws_session.py"), ("Stop", "knowledge_summary.py")])
def test_real_selected_worktree_hook_keeps_native_payload_and_old_pin(family, monkeypatch, capfd, event, hook):
    source, target = family
    selected = ready(target)
    (source / "uv.lock").write_text("version = 1\n# new source package selection\n")
    parent = ready(source)
    (target / "uv.lock").write_text("version = 1\n# unfinished task edit\n")
    nested = target / "subdirectory"
    nested.mkdir()
    settings = {"VAWS_AGENT_SESSIONS_DIR": str(source / "custom registry"),
                "VAWS_COORDINATOR_STATE_DIR": str(source / "custom state")}
    path = target / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('[mcp_servers.vaws_task]\ncommand="custom-provider"\n[mcp_servers.vaws_task.env]\n' +
                    "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items()) +
                    'VAWS_CONTEXT_FILE="not-a-setting"\nVAWS_ENV_RECEIPT="not-the-selected-pin"\n')
    payload = {"hook_event_name": event, "session_id": "native-exact-id", "agent_id": "native-child-id",
               "cwd": str(nested), "source": "resume", "last_assistant_message": "fixture"}
    before = git(target, "status", "--porcelain")
    monkeypatch.setattr(adapter, "ROOT", source)
    monkeypatch.setenv(envs.PIN_ENV, parent["receipt"])
    monkeypatch.setenv(envs.MANAGED_PIN_ENV, parent["receipt"])
    monkeypatch.setenv("VAWS_CONTEXT_FILE", "an-inherited-other-task")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert adapter.main() == 0
    out, err = capfd.readouterr()
    result = json.loads(out)
    assert result["payload"] == payload
    assert result["cwd"] == str(target)
    assert result["python"] == selected["python"]
    assert result["hook"] == hook
    assert result["env"][envs.PIN_ENV] == selected["receipt"]
    assert result["env"]["VAWS_CONTEXT_FILE"] is None
    assert {key: result["env"][key] for key in settings} == settings
    assert result["argv"] == ["--client", "codex", "--project", str(target), "--environment-receipt", selected["receipt"]]
    assert json.loads(err)["workspace"] == str(target)
    assert git(target, "status", "--porcelain") == before


@pytest.mark.parametrize("payload", [{}, {"cwd": "relative/path"}, {"cwd": None}])
def test_missing_native_cwd_does_not_use_process_cwd(family, monkeypatch, payload):
    source, _ = family
    monkeypatch.chdir(source)
    assert adapter.scoped_workspace(payload, source) is None


def test_unrelated_git_repository_is_not_attached(family, tmp_path):
    source, target = family
    other = tmp_path / "unrelated"
    other.mkdir()
    git(other, "init", "-q")
    assert adapter.scoped_workspace({"cwd": str(target)}, source) == target
    assert adapter.scoped_workspace({"cwd": str(source)}, source) == source
    assert adapter.scoped_workspace({"cwd": str(other)}, source) is None


def test_missing_target_environment_reports_fact_without_source_fallback(family, monkeypatch, capsys):
    source, target = family
    ready(source)
    monkeypatch.setattr(adapter, "ROOT", source)
    monkeypatch.setenv("VAWS_ENV_HOME", str(target / "missing-environments"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(target), "session_id": "exact"})))
    assert adapter.main() == 0
    out, err = capsys.readouterr()
    assert not out
    assert json.loads(err)["phase"] == "codex_session_hook"
    assert not (target / ".vaws-local").exists()


def generated(source, project, *, summary=True):
    events = config.EVENTS if summary else config.EVENTS[:-1]
    return {"hooks": {event: [{"hooks": [{"type": "command", "timeout": 12,
        "command": shlex.join(["/old/env/python", str(source / ".agents/hooks" /
            ("knowledge_summary.py" if event == "Stop" else "vaws_session.py")),
            "--client", "codex", "--project", str(project), "--environment-receipt", "/old/pin"])}]}]
        for event in events}}


def plan(files, source, target, home, monkeypatch, *, enable=False):
    monkeypatch.setattr(config, "managed_receipt", lambda _: {"python": "/initial/env/python"})
    notes = []
    config.add_codex_setup(files, notes, target, source, shell_command=shlex.join,
                           parse_command=shlex.split, enable=enable, user_path=home / "hooks.json")
    return notes


def test_user_install_preserves_custom_hooks_and_only_removes_generated(family, tmp_path, monkeypatch):
    source, target = family
    user_path, project_path = tmp_path / "home/hooks.json", target / ".codex/hooks.json"
    custom = {"type": "command", "command": "echo user-custom", "custom": "preserve"}
    project = generated(source, target)
    project["hooks"]["SessionStart"][0]["hooks"].append(custom)
    project["version"] = "user-version"
    user = {"user-field": "keep", "hooks": {"Stop": [{"matcher": "user-matcher", "hooks": [custom]}]}}
    files = {project_path: json.dumps(project), user_path: json.dumps(user)}
    notes = plan(files, source, target, user_path.parent, monkeypatch, enable=True)
    result = json.loads(files[user_path])
    assert result["user-field"] == "keep"
    assert result["hooks"]["Stop"][0] == user["hooks"]["Stop"][0]
    for event in config.EVENTS:
        own = [item for item in config.items(result["hooks"][event]) if config.hook_kind(item, source, shlex.split)]
        assert len(own) == 1
        assert shlex.split(own[0]["command"]) == ["/initial/env/python", str(source / ".agents/scripts/vaws_codex_session.py")]
        assert "pin" not in own[0]["command"]
    assert json.loads(files[project_path]) == {"version": "user-version", "hooks": {
        "SessionStart": [{"hooks": [custom]}]}}
    assert notes and "trust" in notes[0]


def test_next_worktree_keeps_global_definition_bytes_and_bootstrap(family, tmp_path, monkeypatch):
    source, target = family
    user_path = tmp_path / "home/hooks.json"
    files = {source / ".codex/hooks.json": json.dumps(generated(source, source))}
    plan(files, source, source, user_path.parent, monkeypatch, enable=True)
    reviewed = files[user_path]
    target_path = target / ".codex/hooks.json"
    files[target_path] = json.dumps(generated(target, target))
    def forbidden(_):
        pytest.fail("new worktree must not replace reviewed bootstrap with its selected pin")
    monkeypatch.setattr(config, "managed_receipt", forbidden)
    config.add_codex_setup(files, [], target, target, shell_command=shlex.join,
                           parse_command=shlex.split, user_path=user_path)
    assert files[user_path] == reviewed
    assert json.loads(files[target_path]) == {"hooks": {}}
    config.add_codex_setup(files, [], target, target, shell_command=shlex.join,
                           parse_command=shlex.split, user_path=user_path)
    assert files[user_path] == reviewed


def test_user_legacy_generated_hooks_are_replaced_once(family, tmp_path, monkeypatch):
    source, target = family
    user_path, project_path = tmp_path / "home/hooks.json", target / ".codex/hooks.json"
    files = {project_path: json.dumps(generated(source, target)), user_path: json.dumps(generated(source, source))}
    plan(files, source, target, user_path.parent, monkeypatch, enable=True)
    user = json.loads(files[user_path])
    for event in config.EVENTS:
        assert len(list(config.items(user["hooks"][event]))) == 1
        assert config.hook_kind(next(config.items(user["hooks"][event])), source, shlex.split) == "adapter"


def test_custom_shell_wrapper_and_other_project_entry_are_preserved(family, tmp_path, monkeypatch):
    source, target = family
    user_path, project_path = tmp_path / "home/hooks.json", target / ".codex/hooks.json"
    project = generated(source, target)
    foreign = generated(tmp_path / "other", tmp_path / "other")["hooks"]["SessionStart"][0]["hooks"][0]
    chained = {**project["hooks"]["SessionStart"][0]["hooks"][0]}
    chained["command"] += " && echo custom"
    project["hooks"]["SessionStart"][0]["hooks"] += [foreign, chained]
    files = {project_path: json.dumps(project)}
    plan(files, source, target, user_path.parent, monkeypatch, enable=True)
    assert list(config.items(json.loads(files[project_path])["hooks"]["SessionStart"])) == [foreign, chained]


def test_no_global_install_without_initialization_and_task_only_skips_summary(family, tmp_path, monkeypatch):
    source, target = family
    user_path, project_path = tmp_path / "home/hooks.json", target / ".codex/hooks.json"
    files = {project_path: json.dumps(generated(source, target, summary=False))}
    before = dict(files)
    plan(files, source, target, user_path.parent, monkeypatch)
    assert files == before
    plan(files, source, target, user_path.parent, monkeypatch, enable=True)
    assert "Stop" not in json.loads(files[user_path])["hooks"]


def test_client_setup_installs_once_and_next_native_target_only_removes_project_hooks(family, tmp_path, monkeypatch):
    source, target = family
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    spec = importlib.util.spec_from_file_location("codex_hook_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
    setup = importlib.util.module_from_spec(spec)
    with patch("vaws_venv.ensure_workspace_interpreter"):
        spec.loader.exec_module(setup)
    receipt = {"python": sys.executable, "receipt": "initial-selection"}
    settings = {"VAWS_AGENT_SESSIONS_DIR": str(source / "custom registry"), envs.PIN_ENV: receipt["receipt"]}
    monkeypatch.setattr(setup, "ROOT", source)
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", source / ".agents/hooks/vaws_session.py")
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    monkeypatch.setattr(setup, "managed_receipt", lambda _: receipt)
    monkeypatch.setattr(setup, "task_server_env", lambda: settings)
    monkeypatch.setattr(config, "managed_receipt", lambda _: receipt)
    first = setup.build_plan("codex", source, task_only=True, codex_global_hooks=True)
    assert json.loads(first["files"][source / ".codex/hooks.json"]) == {"hooks": {}}
    user_path = home / "hooks.json"
    reviewed = first["files"][user_path]
    home.mkdir()
    user_path.write_text(reviewed)
    # The real selected-revision build_plan entry does not receive the initial
    # user-level opt-in and cannot regenerate the reviewed command or source.
    monkeypatch.setattr(setup, "ROOT", target)
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", target / ".agents/hooks/vaws_session.py")
    receipt["python"], receipt["receipt"] = "/new/selected/python", "new-selection"
    second = setup.build_plan("codex", target, task_only=True)
    assert user_path not in second["files"]
    assert user_path.read_text() == reviewed
    assert json.loads(second["files"][target / ".codex/hooks.json"]) == {"hooks": {}}
    assert target / ".codex/environments/environment.toml" in second["files"]
