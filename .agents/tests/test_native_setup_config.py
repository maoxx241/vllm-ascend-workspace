"""Native setup preserves existing configuration and stays path-independent."""
from __future__ import annotations

import json
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_native_setup_config import add_native_setup


def plan(project, client, files=None):
    files = {} if files is None else files
    notes = []
    add_native_setup(files, notes, client, project, ROOT)
    return files, notes


@pytest.mark.parametrize("client", ["codex", "cursor"])
@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_new_config_is_portable_without_interpolated_paths(tmp_path, monkeypatch, client, platform):
    monkeypatch.setattr(sys, "platform", platform)
    project = tmp_path / "用户's work $HOME `command`"
    files, notes = plan(project, client)
    assert notes == []
    assert len(files) == 1
    text = next(iter(files.values()))
    if client == "codex":
        config = tomllib.loads(text)
        assert config["version"] == 1 and config["name"] == "VAWS"
        command = config["setup"]["script"]
    else:
        command, = json.loads(text)["setup-worktree"]
    argv = shlex.split(command)
    assert argv[:5] == ["uv", "run", "--no-project", "python", "-c"]
    assert argv[-2:] == ["--client", client]
    source_key = "CODEX_SOURCE_TREE_PATH" if client == "codex" else "ROOT_WORKTREE_PATH"
    assert "os.environ['" + source_key + "']" in argv[5]
    assert str(project) not in text
    assert plan(project, client, files)[0] == files


def test_codex_preserves_selected_name_unknown_fields_and_multiline_script(tmp_path):
    directory = tmp_path / ".codex/environments"
    directory.mkdir(parents=True)
    path = directory / "team.toml"
    original = '''# Keep this environment selected.
version = 1
name = "Team environment"
custom = { nested = [1, 2] }

[setup]
script = """
echo "$HOME"
# [setup.win32] is shell text
echo 'user setup'
"""
keep = true

[metadata]
owner = "team"
'''
    path.write_text(original)
    files, notes = plan(tmp_path, "codex")
    assert not notes
    assert set(files) == {path}
    before, after = tomllib.loads(original), tomllib.loads(files[path])
    first, user_script = after["setup"]["script"].split("\n", 1)
    assert first.endswith("--client codex")
    assert user_script == before["setup"]["script"]
    after["setup"]["script"] = before["setup"]["script"]
    assert after == before
    assert files[path].startswith("# Keep this environment selected.")
    once = files.copy()
    plan(tmp_path, "codex", files)
    assert files == once


def test_codex_prepares_before_platform_setup(tmp_path):
    path = tmp_path / ".codex/environments/environment.toml"
    original = '''version = 1
name = "VAWS"
[setup]
script = "echo common"
[setup.win32]
script = "Write-Output 'Windows'"
custom = 7
[setup.darwin]
script = "echo mac"
[setup.linux]
script = "echo linux"
'''
    files, notes = plan(tmp_path, "codex", {path: original})
    assert not notes
    setup = tomllib.loads(files[path])["setup"]
    for block in [setup, *(setup[name] for name in ("win32", "darwin", "linux"))]:
        assert block["script"].splitlines()[0].endswith("--client codex")
    assert setup["win32"]["custom"] == 7


@pytest.mark.parametrize("text", [
    'version=0\nname="unsupported"\n',
    'version=1\nname="Team"\nsetup={script="echo inline"}\n',
    'version=1\nname="Team"\n[setup]\nscript=["unknown"]\n',
    'not valid TOML',
])
def test_codex_unknown_structure_is_preserved(tmp_path, text):
    path = tmp_path / ".codex/environments/environment.toml"
    files, notes = plan(tmp_path, "codex", {path: text})
    assert files[path] == text
    assert notes[0]["reason"] == "native-environment-setup-needs-integration"


def test_codex_multiple_environments_are_not_replaced(tmp_path):
    directory = tmp_path / ".codex/environments"
    existing = {directory / name: 'version=1\nname="Team"\n'
                for name in ("one.toml", "two.toml")}
    files, notes = plan(tmp_path, "codex", existing.copy())
    assert files == existing
    assert notes[0]["reason"] == "multiple-native-environments-selection-unknown"


def test_cursor_preserves_all_commands_and_unknown_fields(tmp_path):
    path = tmp_path / ".cursor/worktrees.json"
    original = {"setup-worktree": ["echo '$HOME'"],
                "setup-worktree-unix": ["echo unix"],
                "setup-worktree-windows": ["Write-Output 'windows'"],
                "future": {"keep": True}}
    files, notes = plan(tmp_path, "cursor", {path: json.dumps(original)})
    assert not notes
    updated = json.loads(files[path])
    for key in ("setup-worktree", "setup-worktree-unix", "setup-worktree-windows"):
        assert updated[key][1:] == original[key]
        assert updated[key][0].endswith("--client cursor")
    assert updated["future"] == original["future"]
    once = files.copy()
    plan(tmp_path, "cursor", files)
    assert files == once


@pytest.mark.parametrize("config", [[], {"setup-worktree": "user-script.sh"},
                                    {"setup-worktree-unix": "user-script.sh"},
                                    {"setup-worktree": [4]}])
def test_cursor_unsupported_setup_does_not_claim_enabled(tmp_path, config):
    path = tmp_path / ".cursor/worktrees.json"
    original = json.dumps(config)
    files, notes = plan(tmp_path, "cursor", {path: original})
    assert files[path] == original
    assert notes[0]["reason"] == "native-worktree-setup-needs-integration"


def test_other_clients_not_configured(tmp_path):
    assert plan(tmp_path, "claude") == ({}, [])


@pytest.mark.parametrize("client", ["codex", "cursor"])
def test_existing_callback_moves_before_user_builds_and_is_deduplicated(tmp_path, client):
    fresh, _ = plan(tmp_path, client)
    path, text = next(iter(fresh.items()))
    if client == "codex":
        command = tomllib.loads(text)["setup"]["script"]
        old_script = "npm ci\n" + command + "\nnpm run build\n" + command
        existing = 'version = 1\nname = "Team"\n[setup]\nscript = ' + json.dumps(old_script) + "\n"
    else:
        command = json.loads(text)["setup-worktree"][0]
        existing = json.dumps({"setup-worktree": ["npm ci", command, "npm run build", command]})
    files, notes = plan(tmp_path, client, {path: existing})
    assert not notes
    commands = (tomllib.loads(files[path])["setup"]["script"].splitlines() if client == "codex"
                else json.loads(files[path])["setup-worktree"])
    assert commands == [command, "npm ci", "npm run build"]
    once = files.copy()
    plan(tmp_path, client, files)
    assert files == once


@pytest.mark.parametrize("client", ["codex", "cursor"])
def test_bootstrap_loads_source_script_with_literal_special_path(tmp_path, monkeypatch, client):
    import subprocess
    source = tmp_path / "用户's source $HOME `literal`"
    target = tmp_path / "old checkout"
    target.mkdir()
    script = source / ".agents/scripts/vaws_worktree_setup.py"
    script.parent.mkdir(parents=True)
    script.write_text("import json,os,sys\nprint(json.dumps([os.getcwd(),sys.argv[1:]]))\n")
    source_key = "CODEX_SOURCE_TREE_PATH" if client == "codex" else "ROOT_WORKTREE_PATH"
    monkeypatch.setenv(source_key, str(source))
    files, _ = plan(source, client)
    text = next(iter(files.values()))
    command = (tomllib.loads(text)["setup"]["script"] if client == "codex"
               else json.loads(text)["setup-worktree"][0])
    argv = shlex.split(command)
    result = subprocess.run([sys.executable, *argv[4:]], cwd=target, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [str(target), ["--client", client]]
