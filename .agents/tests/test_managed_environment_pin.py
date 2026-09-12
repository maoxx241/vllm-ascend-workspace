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


@pytest.mark.parametrize('mode', ['wsl', 'windows', 'native-linux'])
def test_new_client_clears_parent_pins_and_selects_current_native_and_owner(tmp_path, monkeypatch, mode):
    checkout, copy = tmp_path / 'checkout', tmp_path / 'new copy'
    project(checkout)
    copy.mkdir()
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
    monkeypatch.setattr(client, 'prepare_workspace', lambda *args, **kwargs: {'state': 'reused', 'workspace': str(copy)})
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
    child = seen[0]
    assert child[envs.PIN_ENV] == native['receipt']
    assert child['VAWS_AGENT_SESSIONS_DIR'] == owner.accessible_windows_path(registry)
    if mode == 'native-linux':
        assert envs.MANAGED_PIN_ENV not in child
    else:
        assert child[envs.MANAGED_PIN_ENV] == current_owner['receipt'] != old_owner['receipt']
        selected = json.loads((copy / '.vaws-local/environment-selection/win32.json').read_text())
        assert selected['key'] == current_owner['key']
