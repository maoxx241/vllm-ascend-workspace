"""Business CLIs retain their native task after shell directory changes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import vaws_coordinator_launch
from vaws_coordinator.agent_session import AgentSessions, load_context
from vaws_task_target import resolve_context_file


@pytest.mark.parametrize('location', ['nested-repository', 'outside-workspace'])
def test_skill_uses_workspace_registry_after_shell_cd(tmp_path, monkeypatch, location):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    cwd = workspace / 'vllm' if location == 'nested-repository' else tmp_path / 'outside'
    cwd.mkdir()
    if location == 'nested-repository':
        subprocess.run(['git', 'init', '-q', str(cwd)], check=True)
    registry = workspace / '.vaws-local/agent-sessions'
    store = AgentSessions(registry)
    context = store.attach('codex', 'native-skill-session', str(workspace))
    monkeypatch.setenv('VAWS_AGENT_SESSIONS_DIR', '')  # Restore later CLI normalization too.
    for key in ('VAWS_AGENT_SESSIONS_DIR', 'VAWS_CONTEXT_FILE', 'VAWS_PARENT_CONTEXT',
                'VAWS_ATTACH_CONTEXT', 'CODEX_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CODEX_THREAD_ID', 'native-skill-session')
    environment = vaws_coordinator_launch.coordinator_environment
    monkeypatch.setattr(vaws_coordinator_launch, 'coordinator_environment',
                        lambda: environment(repo_root=workspace))
    monkeypatch.chdir(cwd)
    resolved = load_context(resolve_context_file())
    assert resolved['session']['id'] == context['session']['id']
    assert Path(resolved['state_dir']) == registry.resolve()
    assert not (cwd / '.vaws-local/agent-sessions').exists()


def test_skill_preserves_explicit_registry_and_context(tmp_path, monkeypatch):
    registry = tmp_path / 'explicit-registry'
    store = AgentSessions(registry)
    context = store.attach('codex', 'explicit-native', str(tmp_path))
    monkeypatch.setenv('VAWS_AGENT_SESSIONS_DIR', str(registry))
    monkeypatch.setenv('CODEX_THREAD_ID', 'explicit-native')
    for key in ('VAWS_CONTEXT_FILE', 'VAWS_PARENT_CONTEXT', 'VAWS_ATTACH_CONTEXT', 'CODEX_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    assert resolve_context_file() == context['context_file']
    another = AgentSessions(tmp_path / 'associated-registry').attach('claude', 'associated', str(tmp_path))
    assert resolve_context_file(another['context_file']) == another['context_file']
    assert os.environ['VAWS_AGENT_SESSIONS_DIR'] == str(registry)
