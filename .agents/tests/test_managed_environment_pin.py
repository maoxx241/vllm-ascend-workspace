"""Native and Windows owner receipts stay separate across client lifetimes."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import vaws_environment as envs
import vaws_local_owner as owner
import vaws_workspace_entry as entry

ROOT = Path(__file__).resolve().parents[2]


def project(path: Path):
    path.mkdir()
    (path / 'pyproject.toml').write_text("[project]\nname='pin-fixture'\nversion='0'\n[tool.uv]\npackage=false\n", encoding='utf-8')
    (path / 'uv.lock').write_text('version = 1\n', encoding='utf-8')


def ready_fixture(root: Path, target_platform: str) -> dict:
    # These are validated receipt fixtures, not installed or executable Python
    # environments. Environment installation/liveness has separate real tests.
    _, _, document, input_id, lock_sha = envs._inputs(root)
    selection = envs._selection(document)[0]
    identity = {'platform': target_platform, 'arch': 'amd64', 'abi': 'fixture-abi',
                'python_version': '3.13.12', 'implementation': 'cpython',
                'cache_tag': 'cpython-313', 'sysconfig_platform': target_platform, 'build': 'fixture'}
    key = envs._key(identity, input_id, selection)
    store = root.parent / 'ready-store'
    directory = store / key
    python = directory / ('Scripts/python.exe' if target_platform == 'win32' else 'bin/python')
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text('receipt fixture only', encoding='utf-8')
    receipt = {'schema_version': 1, 'recipe_version': envs.RECIPE_VERSION, 'key': key,
               'root': str(directory.resolve()), 'python': str(python.resolve()), 'base_python': str(python.resolve()),
               'python_identity': identity, 'input_id': input_id, 'lock_sha256': lock_sha,
               'selection': selection, 'store': str(store.resolve()),
               'receipt': str((directory / envs.READY_NAME).resolve()),
               **{name: identity[name] for name in ('platform', 'arch', 'abi', 'python_version')}}
    (directory / envs.READY_NAME).write_text(json.dumps(receipt), encoding='utf-8')
    envs.select_environment(root, receipt)
    return receipt


def test_running_windows_owner_pin_survives_lock_and_selection_changes(tmp_path, monkeypatch):
    checkout = tmp_path / 'checkout'
    project(checkout)
    native = ready_fixture(checkout, 'linux')
    first = ready_fixture(checkout, 'win32')
    monkeypatch.setenv(envs.PIN_ENV, native['receipt'])
    monkeypatch.setenv(envs.MANAGED_PIN_ENV, first['receipt'])
    assert envs.windows_ready(checkout) == first
    (checkout / 'uv.lock').write_text('version = 1\n# new locked inputs\n', encoding='utf-8')
    second = ready_fixture(checkout, 'win32')
    assert second['key'] != first['key']
    assert envs.windows_ready(checkout) == first
    monkeypatch.delenv(envs.MANAGED_PIN_ENV)
    assert envs.windows_ready(checkout) == second
    monkeypatch.setenv(envs.MANAGED_PIN_ENV, native['receipt'])
    with pytest.raises(envs.EnvironmentError, match='expected win32'):
        envs.windows_ready(checkout)


def test_saved_ready_uses_checkout_selection_despite_changed_lock_and_parent_pins(tmp_path, monkeypatch):
    checkout, parent = tmp_path / 'checkout', tmp_path / 'parent'
    project(checkout)
    project(parent)
    native = ready_fixture(checkout, sys.platform)
    managed = ready_fixture(checkout, 'win32')
    (checkout / 'uv.lock').write_text('version = 1\n# session edits\n', encoding='utf-8')
    (parent / 'uv.lock').write_text('version = 1\n# newer upstream\n', encoding='utf-8')
    parent_native = ready_fixture(parent, sys.platform)
    parent_managed = ready_fixture(parent, 'win32')
    monkeypatch.setenv(envs.PIN_ENV, parent_native['receipt'])
    monkeypatch.setenv(envs.MANAGED_PIN_ENV, parent_managed['receipt'])
    assert envs.saved_ready(checkout) == native
    assert envs.saved_ready(checkout, target_platform='win32') == managed


def test_saved_ready_without_selection_looks_up_native_but_requires_windows_configuration(tmp_path, monkeypatch):
    checkout = tmp_path / 'checkout'
    project(checkout)
    native = ready_fixture(checkout, 'linux')
    (checkout / '.vaws-local/environment-selection/linux.json').unlink()
    monkeypatch.setattr(envs, '_identity', lambda: native['python_identity'])
    monkeypatch.setenv('VAWS_ENV_HOME', native['store'])
    monkeypatch.setenv(envs.PIN_ENV, 'unrelated-parent-receipt')
    monkeypatch.setenv(envs.MANAGED_PIN_ENV, 'unrelated-parent-owner-receipt')
    assert envs.saved_ready(checkout, target_platform='linux') == native
    with pytest.raises(envs.EnvironmentError, match='no Windows environment selection'):
        envs.saved_ready(checkout, target_platform='win32')


@pytest.mark.parametrize('mode', ['wsl', 'windows', 'native-linux'])
def test_new_client_clears_parent_pins_and_selects_current_native_and_owner(tmp_path, monkeypatch, mode):
    checkout, copy = tmp_path / 'checkout', tmp_path / 'new copy'
    project(checkout)
    old_native = ready_fixture(checkout, 'linux')
    old_owner = ready_fixture(checkout, 'win32')
    (checkout / 'uv.lock').write_text('version = 1\n# new client lock\n', encoding='utf-8')
    native = ready_fixture(checkout, 'win32' if mode == 'windows' else 'linux')
    current_owner = native if mode == 'windows' else ready_fixture(checkout, 'win32')
    spec = importlib.util.spec_from_file_location('pin_client_fixture', ROOT / '.agents/scripts/vaws_client.py')
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    monkeypatch.setattr(client, 'ROOT', checkout)
    monkeypatch.setattr(client, 'resolve_client', lambda name: ['native-client'])
    monkeypatch.setattr(client, 'prepare_workspace', lambda *args, **kwargs: {'state': 'ready', 'workspace': str(copy)})
    checks = []
    monkeypatch.setattr(entry, 'prepare_session', lambda root: checks.append(root) or {'state': 'configured'})
    monkeypatch.setenv('VAWS_RELEASE_LAUNCH', '0')
    monkeypatch.setenv(envs.PIN_ENV, old_native['receipt'])
    monkeypatch.setenv(envs.MANAGED_PIN_ENV, old_owner['receipt'])
    def enter_native(**kwargs):
        assert envs.PIN_ENV not in os.environ and envs.MANAGED_PIN_ENV not in os.environ
        os.environ[envs.PIN_ENV] = native['receipt']
    monkeypatch.setattr(client, 'ensure_workspace_interpreter', enter_native)
    monkeypatch.setattr(envs, 'native_ready', lambda root: native)
    monkeypatch.setattr(owner, 'windows_mounted_workspace', lambda root: mode == 'wsl')
    registry = str(checkout / '.vaws-local/agent-sessions')
    setup = SimpleNamespace(build_plan=lambda *args: {}, apply_plan=lambda plan: {},
                            launch_env=lambda *args: {'VAWS_AGENT_SESSIONS_DIR': registry})
    monkeypatch.setitem(sys.modules, 'vaws_client_setup', setup)
    seen = []
    monkeypatch.setattr(client, 'run_client', lambda command, cwd, environment: seen.append(environment) or 0)
    assert client.main(['codex', '--workspace', str(copy)]) == 0
    assert checks == [checkout]
    child = seen[0]
    assert child[envs.PIN_ENV] == native['receipt']
    assert child['VAWS_AGENT_SESSIONS_DIR'] == owner.accessible_windows_path(registry)
    if mode == 'native-linux':
        assert envs.MANAGED_PIN_ENV not in child
    else:
        assert child[envs.MANAGED_PIN_ENV] == current_owner['receipt'] != old_owner['receipt']
        selected = json.loads((copy / '.vaws-local/environment-selection/win32.json').read_text())
        assert selected['key'] == current_owner['key']


@pytest.mark.parametrize('mode', ['wsl', 'windows', 'native-linux'])
def test_existing_workspace_resume_keeps_saved_pins_and_configuration(tmp_path, monkeypatch, mode):
    checkout, target = tmp_path / 'checkout', tmp_path / 'existing session'
    project(checkout)
    project(target)
    native_platform = 'win32' if mode == 'windows' else 'linux'
    native = ready_fixture(target, native_platform)
    managed = native if mode == 'windows' else ready_fixture(target, 'win32')
    (target / 'uv.lock').write_text('version = 1\n# unfinished session edits\n', encoding='utf-8')
    (checkout / 'uv.lock').write_text('version = 1\n# newer upstream inputs\n', encoding='utf-8')
    parent_native = ready_fixture(checkout, native_platform)
    parent_managed = parent_native if mode == 'windows' else ready_fixture(checkout, 'win32')
    assert parent_native['key'] != native['key']
    registry = str(target / '.vaws-local/original-agent-sessions')
    config = target / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text('# original session configuration\n', encoding='utf-8')
    original = {path: path.read_bytes() for path in (target / '.vaws-local/environment-selection').iterdir()}
    original[config] = config.read_bytes()
    spec = importlib.util.spec_from_file_location('resume_pin_client_fixture', ROOT / '.agents/scripts/vaws_client.py')
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    monkeypatch.setattr(client, 'ROOT', checkout)
    monkeypatch.setattr(client, 'resolve_client', lambda name: ['native-client'])
    monkeypatch.setattr(client, 'prepare_workspace', lambda *args, **kwargs: {'state': 'reused', 'workspace': str(target)})
    monkeypatch.setattr(sys, 'platform', native_platform)
    monkeypatch.setenv('VAWS_RELEASE_LAUNCH', '0')
    monkeypatch.setenv(envs.PIN_ENV, parent_native['receipt'])
    monkeypatch.setenv(envs.MANAGED_PIN_ENV, parent_managed['receipt'])
    monkeypatch.setattr(owner, 'windows_mounted_workspace', lambda root: mode == 'wsl')

    def unexpected(*args, **kwargs):
        pytest.fail('resume must not prepare updates, reselect environments or rewrite client configuration')

    entered = []
    def enter_native(**kwargs):
        assert kwargs['repo_root'] == target
        assert os.environ[envs.PIN_ENV] == native['receipt']
        entered.append(kwargs['repo_root'])

    monkeypatch.setattr(client, 'ensure_workspace_interpreter', enter_native)
    monkeypatch.setattr(entry, 'prepare_session', unexpected)
    monkeypatch.setattr(envs, 'select_environment', unexpected)
    setup = SimpleNamespace(build_plan=unexpected, apply_plan=unexpected, launch_env=unexpected,
                            existing_task_env=lambda *args: {'VAWS_AGENT_SESSIONS_DIR': registry})
    monkeypatch.setitem(sys.modules, 'vaws_client_setup', setup)
    seen = []
    monkeypatch.setattr(client, 'run_client', lambda command, cwd, environment: seen.append((command, cwd, environment)) or 0)
    assert client.main(['codex', '--workspace', str(target), '--', 'resume', 'original-session']) == 0
    assert entered == [target]
    command, cwd, child = seen[0]
    assert command == ['native-client', 'resume', 'original-session']
    assert cwd == target
    assert child[envs.PIN_ENV] == native['receipt']
    assert child['VAWS_AGENT_SESSIONS_DIR'] == owner.accessible_windows_path(registry)
    if mode == 'native-linux':
        assert envs.MANAGED_PIN_ENV not in child
    else:
        assert child[envs.MANAGED_PIN_ENV] == managed['receipt'] != parent_managed['receipt']
    assert {path: path.read_bytes() for path in original} == original
