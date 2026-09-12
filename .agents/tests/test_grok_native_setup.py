"""Grok Git callback scope and coexistence use real local repositories."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
sys.path.insert(0, str(ROOT / ".agents/scripts"))
from vaws_grok_setup_config import MARKER, plan_grok_setup
import vaws_grok_worktree as callback


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path):
    source = tmp_path / "source 用户's repo $shell"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README").write_text("fixture\n")
    git(source, "add", ".")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    home = tmp_path / "grok home"
    target = home / "worktrees/project/new"
    target.parent.mkdir(parents=True)
    git(source, "worktree", "add", "--detach", str(target), "HEAD")
    return source, target, home, git(source, "rev-parse", "HEAD")


def test_only_new_native_git_worktree_qualifies(repository):
    source, target, home, revision = repository
    assert callback.creation_target(source, target, "0" * 40, revision, "1", home)
    assert not callback.creation_target(source, source, "0" * 40, revision, "1", home)
    assert not callback.creation_target(source, target, revision, revision, "1", home)
    assert not callback.creation_target(source, target, "0" * 40, revision, "0", home)
    assert not callback.creation_target(source, target, "0" * 40, revision, "1", home / "other")
    assert not callback.creation_target(source, target, "0" * 40, "bad-head", "1", home)


def test_unrelated_repository_is_not_adopted(repository, tmp_path):
    source, target, home, revision = repository
    other = home / "worktrees/unrelated"
    subprocess.run(["git", "clone", str(source), str(other)], check=True, capture_output=True)
    assert not callback.creation_target(source, other, "0" * 40, revision, "1", home)


def test_callback_prepares_before_return_and_clears_parent_pins(repository, monkeypatch, capsys):
    source, target, home, revision = repository
    monkeypatch.chdir(target)
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setenv(callback.PIN_ENV, "parent-receipt")
    monkeypatch.setenv(callback.MANAGED_PIN_ENV, "parent-managed-receipt")
    calls = []
    def prepare(client, actual_source, actual_target):
        assert callback.PIN_ENV not in os.environ
        assert callback.MANAGED_PIN_ENV not in os.environ
        calls.append((client, actual_source, actual_target))
        return {"status": "ready", "environment": "fixed"}
    monkeypatch.setattr(callback, "prepare_worktree", prepare)
    assert callback.main(["--source", str(source), "0" * 40, revision, "1"]) == 0
    assert calls == [("grok", source, target)]
    assert '"status": "ready"' in capsys.readouterr().err
    assert git(target, "rev-parse", "HEAD") == revision
    assert git(source, "symbolic-ref", "--short", "HEAD") == "main"


def test_failure_returns_native_creation_error(repository, monkeypatch, capsys):
    source, target, home, revision = repository
    monkeypatch.chdir(target)
    monkeypatch.setenv("GROK_HOME", str(home))
    def fail(*args):
        raise RuntimeError("dependency preparation failed: fixture evidence")
    monkeypatch.setattr(callback, "prepare_worktree", fail)
    assert callback.main(["--source", str(source), "0" * 40, revision, "1"]) == 1
    assert "fixture evidence" in capsys.readouterr().err


def test_config_preserves_global_preferences_and_quotes_paths(repository):
    source, target, home, revision = repository
    files, notes = {}, []
    executable = plan_grok_setup(files, notes, source, ROOT)
    hook = source / ".git/hooks/post-checkout"
    assert executable == [hook] and list(files) == [hook]
    assert MARKER in files[hook]
    assert all("config.toml" not in str(path) for path in files)
    assert notes[0]["reason"] == "grok-native-worktree-preferences"
    # Ordinary checkout never starts Python or uv, even if PATH contains neither.
    result = subprocess.run(["/bin/sh", "-c", files[hook], "post-checkout", revision, revision, "1"],
                            env={"PATH": "/nonexistent"}, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    once = dict(files)
    assert plan_grok_setup(files, [], source, ROOT) == [hook]
    assert files == once


@pytest.mark.parametrize("kind", ["hook", "hooks-path", "symlink"])
def test_existing_hook_owner_is_preserved(repository, tmp_path, kind):
    source, target, home, revision = repository
    hook = source / ".git/hooks/post-checkout"
    if kind == "hooks-path":
        git(source, "config", "core.hooksPath", str(tmp_path / "shared-hooks"))
    elif kind == "symlink":
        hook.symlink_to(tmp_path / "foreign-hook")
    else:
        hook.write_text("#!/bin/sh\necho user hook\n")
    files, notes = {}, []
    assert plan_grok_setup(files, notes, source, ROOT) == []
    assert files == {} and notes[0]["action"] == "preserved"


def test_target_configuration_does_not_retarget_shared_git_hook(repository):
    source, target, home, revision = repository
    files, notes = {}, []
    assert plan_grok_setup(files, notes, target, ROOT) == []
    assert files == {} and notes == []
