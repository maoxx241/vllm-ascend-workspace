"""The managed owner is entered before business I/O, with literal native inputs."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.agents/lib'))
import vaws_managed_entry as entry


def receipt():
    return {'python': r'C:\Users\operator\environments\key\Scripts\python.exe',
            'receipt': r'C:\Users\operator\environments\key\.vaws-ready.json', 'key': 'key'}


def test_only_declared_local_arguments_are_translated():
    remote = "printf '%s' '/mnt/d/remote weight' && python -c 'print(1)'"
    args = ['--context-file=/mnt/d/work/context.json', '--output', '/mnt/e/result 中文.json',
            '--model', '/mnt/d/remote-model', '--wrap-script', remote,
            '--serve-args', '--context-file', '/mnt/d/remote-context']
    converted = entry.local_arguments(args, ('--output',))
    assert converted[:3] == ['--context-file=D:\\work\\context.json', '--output', 'E:\\result 中文.json']
    assert converted[3:] == args[3:]
    with pytest.raises(entry.ManagedEntryError, match='mounted Windows drive'):
        entry.local_arguments(['--context-file', '/tmp/private-context.json'], ())


def test_invocation_preserves_flags_module_arguments_and_explicit_owner_environment():
    environment = {'VAWS_AGENT_SESSIONS_DIR': '/mnt/d/shared/agent-sessions',
                   'VAWS_GITHUB_IDENTITY_FILE': '/mnt/d/shared/github.json',
                   'VAWS_COORDINATOR_STATE_DIR': '/mnt/d/shared/coordinator',
                   'VAWS_CONTEXT_FILE': '/mnt/d/shared/context.json',
                   'CODEX_THREAD_ID': 'this-native-session', 'REMOTE_DEV_DEFAULT_USER': 'root',
                   'REMOTE_DEV_STATE_DIR': '/mnt/d/shared/remote-dev',
                   'PYTHONPATH': '/linux/only:/another', 'PYTHONHOME': '/linux/python',
                   'VIRTUAL_ENV': '/linux/venv', 'VAWS_VENV_REEXEC': 'old-linux-key',
                   'WSLENV': 'USER_CHOICE/w', 'OTHER_VALUE': '/mnt/d/untouched'}
    arguments, child = entry.managed_invocation(__file__, receipt(), environment=environment,
        original=['python', '-B', '-X', 'dev', '-W', 'error', '-m', 'business.cli',
                  '--context-file', '/mnt/d/shared/context.json', '--command', 'cat /mnt/d/remote'])
    assert arguments[:9] == ['/mnt/c/Users/operator/environments/key/Scripts/python.exe',
                              '-X', 'utf8', '-B', '-X', 'dev', '-W', 'error', '-m']
    assert arguments[9:] == ['business.cli', '--context-file', 'D:\\shared\\context.json',
                             '--command', 'cat /mnt/d/remote']
    assert child['VAWS_AGENT_SESSIONS_DIR'] == r'D:\shared\agent-sessions'
    assert child['VAWS_GITHUB_IDENTITY_FILE'] == r'D:\shared\github.json'
    assert child['VAWS_COORDINATOR_STATE_DIR'] == r'D:\shared\coordinator'
    assert child['VAWS_CONTEXT_FILE'] == r'D:\shared\context.json'
    assert child['REMOTE_DEV_STATE_DIR'] == r'D:\shared\remote-dev'
    assert child['VAWS_ENV_RECEIPT'] == receipt()['receipt']
    assert child['VAWS_MANAGED_ENV_RECEIPT'] == receipt()['receipt']
    assert child['CODEX_THREAD_ID'] == 'this-native-session'
    assert child['OTHER_VALUE'] == '/mnt/d/untouched'
    assert {'VAWS_CONTEXT_FILE/w', 'CODEX_THREAD_ID/w', 'VAWS_ENV_RECEIPT/w',
            'VAWS_MANAGED_ENV_RECEIPT/w', 'USER_CHOICE/w'} <= set(child['WSLENV'].split(':'))
    assert not {'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'VAWS_VENV_REEXEC'} & child.keys()


def test_inline_python_is_never_replayed():
    with pytest.raises(entry.ManagedEntryError, match='before reading stdin'):
        entry.managed_invocation(__file__, receipt(), original=['python', '-c', 'already_running()'])


def test_missing_owner_identity_clears_windows_parent_environment():
    environment = {'WSLENV': 'CODEX_THREAD_ID/u:VAWS_CONTEXT_FILE/p:VAWS_ENV_RECEIPT/u:VAWS_MANAGED_ENV_RECEIPT/up:USER_CHOICE/up'}
    _, child = entry.managed_invocation(__file__, receipt(), environment=environment,
                                       original=['python', '-m', 'business.cli'])
    fixed = entry.PATH_ENV | entry.IDENTITY_ENV
    assert all(child[key] == '' for key in fixed)
    entries = set(child['WSLENV'].split(':'))
    assert {key + '/w' for key in fixed} <= entries
    assert 'CODEX_THREAD_ID/u' not in entries
    assert 'VAWS_CONTEXT_FILE/p' not in entries
    assert 'VAWS_ENV_RECEIPT/u' not in entries
    assert 'VAWS_MANAGED_ENV_RECEIPT/up' not in entries
    assert {'VAWS_ENV_RECEIPT/w', 'VAWS_MANAGED_ENV_RECEIPT/w'} <= entries
    assert 'USER_CHOICE/up' in entries


def test_native_execution_does_not_look_up_or_enter_another_owner(monkeypatch):
    monkeypatch.setattr(entry, 'windows_mounted_workspace', lambda _: False)
    monkeypatch.setattr(entry.os, 'execve', lambda *args: pytest.fail('native CLI changed owner'))
    entry.ensure_managed_entry(repo_root=ROOT, entry_file=__file__)


def test_cli_help_needs_no_managed_environment(monkeypatch):
    monkeypatch.setattr(entry, 'windows_mounted_workspace', lambda _: True)
    monkeypatch.setattr(sys, 'argv', ['serving.py', 'start', '--help'])
    monkeypatch.setattr(entry.os, 'execve', lambda *args: pytest.fail('help entered a different owner'))
    entry.ensure_managed_entry(repo_root=ROOT, entry_file=__file__)


def test_imported_business_code_cannot_restart_its_caller(monkeypatch):
    monkeypatch.setattr(entry, 'windows_mounted_workspace', lambda _: True)
    monkeypatch.setitem(sys.modules, '__main__', SimpleNamespace(__file__=str(ROOT / 'another_entry.py')))
    monkeypatch.setattr(entry.os, 'execve', lambda *args: pytest.fail('running business function was replayed'))
    with pytest.raises(entry.ManagedEntryError, match='imported business module'):
        entry.ensure_managed_entry(repo_root=ROOT, entry_file=__file__)


def test_wrong_owner_refuses_before_context_discovery(monkeypatch):
    import vaws_task_target
    import vaws_coordinator.agent_session
    monkeypatch.setattr(entry, 'windows_mounted_workspace', lambda _: True)
    monkeypatch.setattr(vaws_coordinator.agent_session, 'load_context', lambda _: pytest.fail('created or read a context under the wrong owner'))
    with pytest.raises(vaws_task_target.TaskTargetError, match='CLI must enter'):
        vaws_task_target.resolve_context_file()


def test_profile_analysis_cli_enters_owner_before_loading_manifest(tmp_path):
    script = ROOT / '.agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py'
    model = tmp_path / 'model config.json'
    model.write_text('{}', encoding='utf-8')
    manifest = tmp_path / 'does-not-exist.json'
    probe = '''import json, os, runpy, sys
sys.path.insert(0, sys.argv[1])
import vaws_managed_entry
def enter(**kwargs):
    print(json.dumps({key: str(value) if key != 'local_options' else value for key, value in kwargs.items()}))
    raise SystemExit(73)
vaws_managed_entry.ensure_managed_entry = enter
os.environ['VAWS_SKIP_VENV_REEXEC'] = '1'
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', probe,
                             str(ROOT / '.agents/lib'), str(script),
                             '--manifest', str(manifest), '--model-config', str(model),
                             '--hardware-profile', '/remote/hardware.json'],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 73, result.stderr
    owner = json.loads(result.stdout)
    assert Path(owner['repo_root']) == ROOT
    assert Path(owner['entry_file']) == script
    assert owner['local_options'] == ['--manifest', '--local-output-dir', '--model-config']
    assert not manifest.exists()


@pytest.mark.skipif(os.name == 'nt', reason='only WSL/POSIX uses exec replacement; native Windows keeps its owner')
def test_process_replacement_reads_stdin_once_and_performs_first_effect_once(tmp_path):
    # The owner selection is a fixture so this lifecycle check can run on all
    # native CI platforms; separate WSL acceptance uses a real Windows receipt.
    script = tmp_path / 'entry 中文.py'
    effect = tmp_path / 'effect.txt'
    script.write_text('''import json, os, pathlib, sys
sys.path.insert(0, ''' + repr(str(ROOT / '.agents/lib')) + ''')
import vaws_managed_entry as entry
import vaws_environment
if os.environ.get('OWNER_FIXTURE_CHILD') != '1':
    entry.windows_mounted_workspace = lambda root: True
    vaws_environment.windows_ready = lambda root, **kwargs: {'python': sys.executable}
    def invocation(*args, **kwargs):
        return [sys.executable, '-B', '-X', 'utf8', __file__, *sys.argv[1:]], {**os.environ, 'OWNER_FIXTURE_CHILD': '1'}
    entry.managed_invocation = invocation
    entry.ensure_managed_entry(repo_root=pathlib.Path(__file__).parent, entry_file=__file__)
with pathlib.Path(sys.argv[1]).open('a', encoding='utf-8') as stream:
    stream.write('first effect\\n')
print(json.dumps({'stdin': sys.stdin.read(), 'command': sys.argv[2], 'no_bytecode': sys.dont_write_bytecode}, ensure_ascii=False))
''', encoding='utf-8')
    remote = "printf '%s' '/mnt/d/remote 中文'"
    result = subprocess.run([sys.executable, str(script), str(effect), remote],
                            input='input 中文\nsecond line\n', capture_output=True, text=True,
                            encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'stdin': 'input 中文\nsecond line\n', 'command': remote, 'no_bytecode': True}
    assert effect.read_text(encoding='utf-8') == 'first effect\n'
