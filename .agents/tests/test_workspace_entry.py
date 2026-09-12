"""Startup must stay offline and cannot mutate a live GUI checkout."""
import json
from pathlib import Path
import sys
import types

import vaws_workspace_entry as entry
import importlib.util
import os
import pytest
import subprocess
import venv


def configure(root):
    path = root / ".vaws-local/github.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": "vaws.github.v1", "login": "alice"}))


def test_first_use_does_not_probe_network_and_notice_is_once(tmp_path, monkeypatch):
    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network/child")))
    assert entry.workspace_entry(tmp_path)["state"] == "needs_github_user"
    assert entry.workspace_entry(tmp_path)["state"] == "identity_pending"
    assert not (tmp_path / ".vaws-local/github.json").exists()


def test_hidden_gui_hook_does_not_consume_visible_first_use_notice(tmp_path):
    assert entry.workspace_entry(tmp_path, announce=False)["state"] == "identity_pending"
    assert not (tmp_path / ".vaws-local/updates/onboarding-notice.json").exists()
    assert entry.workspace_entry(tmp_path)["state"] == "needs_github_user"


def test_gui_starts_only_background_watcher_and_throttles(tmp_path, monkeypatch):
    root = tmp_path / "工作目录 with spaces"
    configure(root)
    calls = []
    monkeypatch.setattr(entry.subprocess, "Popen", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or types.SimpleNamespace(pid=123))
    monkeypatch.setitem(sys.modules, "vaws_workspace_update", types.SimpleNamespace(
        activate_prepared=lambda *a: (_ for _ in ()).throw(AssertionError("GUI activation"))))
    assert entry.workspace_entry(root)["watcher"] == "requested"
    assert entry.workspace_entry(root)["state"] == "configured"
    assert len(calls) == 1
    command, options = calls[0]
    assert command[-3:] == ["--root", str(root.resolve()), "watch"]
    assert options["stdin"] == entry.subprocess.DEVNULL
    assert "VAWS_CONTEXT_FILE" not in options["env"]
    assert options.get("shell") is None


def test_entry_never_activates_existing_checkout(tmp_path, monkeypatch):
    configure(tmp_path)
    order = []
    monkeypatch.setitem(sys.modules, "vaws_workspace_update", types.SimpleNamespace(
        activate_prepared=lambda root: order.append("activate") or {"state": "applied"}))
    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **k: order.append("watch") or types.SimpleNamespace(pid=123))
    assert entry.workspace_entry(tmp_path)["state"] == "configured"
    assert order == ["watch"]


def test_disabled_or_invalid_config_does_not_break_startup(tmp_path, monkeypatch):
    configure(tmp_path)
    state = tmp_path / ".vaws-local/updates"
    state.mkdir()
    config = state / "config.json"
    monkeypatch.setattr(entry.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("child")))
    config.write_text('{"enabled": false}')
    assert entry.workspace_entry(tmp_path)["state"] == "disabled"
    config.write_text('{')
    assert entry.workspace_entry(tmp_path)["state"] == "unavailable"


def test_watcher_does_not_inherit_client_identity_or_release_flags(tmp_path, monkeypatch):
    configure(tmp_path)
    for key in ("VAWS_RELEASE_LAUNCH", "VAWS_ENV_RECEIPT", "VAWS_MANAGED_ENV_RECEIPT",
                "VAWS_CONTEXT_FILE", "VAWS_PARENT_CONTEXT", "CODEX_THREAD_ID", "VAWS_VENV_REEXEC"):
        monkeypatch.setenv(key, "parent-value")
    calls = []
    monkeypatch.setattr(entry.subprocess, "Popen", lambda cmd, **kwargs: calls.append(kwargs) or types.SimpleNamespace(pid=123))
    assert entry.workspace_entry(tmp_path)["watcher"] == "requested"
    assert all(key not in calls[0]["env"] for key in ("VAWS_RELEASE_LAUNCH", "VAWS_ENV_RECEIPT",
               "VAWS_MANAGED_ENV_RECEIPT", "VAWS_CONTEXT_FILE", "VAWS_PARENT_CONTEXT", "CODEX_THREAD_ID", "VAWS_VENV_REEXEC"))


def test_windows_owner_watcher_uses_native_paths(tmp_path, monkeypatch):
    configure(tmp_path)
    import vaws_local_owner as owner
    import vaws_environment as envs
    monkeypatch.setattr(owner, "windows_mounted_workspace", lambda _: True)
    monkeypatch.setattr(owner, "accessible_windows_path", lambda value: "/mnt/c/Python/python.exe")
    monkeypatch.setattr(owner, "managed_path", lambda value, windows: "C:\\Workspace\\" + Path(value).name)
    monkeypatch.setattr(envs, "windows_ready", lambda _: {"python": "C:\\Python\\python.exe"})
    monkeypatch.setenv("VAWS_ENV_RECEIPT", "stale")
    monkeypatch.setenv("WSLENV", "VAWS_ENV_RECEIPT/w:VISIBLE/w")
    monkeypatch.setenv("VISIBLE", "retained")
    calls = []
    monkeypatch.setattr(entry.subprocess, "Popen", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or types.SimpleNamespace(pid=123))
    assert entry.workspace_entry(tmp_path)["watcher"] == "requested"
    command, options = calls[0]
    assert command[0] == "/mnt/c/Python/python.exe"
    assert command[1] == "C:\\Workspace\\workspace_update.py"
    assert command[-1] == "watch"
    assert options["env"]["WSLENV"] == "VISIBLE/w"


@pytest.fixture
def client_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    stage = tmp_path / "release"
    target = tmp_path / "copy"
    for path in (root, stage, target):
        path.mkdir()
    launcher = stage / ".agents/scripts/vaws_client.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("# fixture release launcher\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("entry_client_fixture", Path(__file__).resolve().parents[1] / "scripts/vaws_client.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    monkeypatch.setattr(client, "ROOT", root)
    monkeypatch.setattr(client, "resolve_client", lambda _: ["native-client"])
    monkeypatch.setattr(client, "prepare_workspace", lambda *a, **k: {"workspace": str(target)})
    monkeypatch.setattr(client, "ensure_workspace_interpreter", lambda **kwargs: None)
    monkeypatch.setattr(entry, "report_workspace_entry", lambda *a, **k: None)
    import vaws_environment as envs
    import vaws_workspace_update as updates
    monkeypatch.setattr(updates, "prepared_source", lambda _: stage)
    receipt = {"python": sys.executable, "root": sys.prefix, "platform": sys.platform, "receipt": "new-pin", "key": "new"}
    monkeypatch.setattr(envs, "native_ready", lambda _: receipt)
    monkeypatch.setattr(envs, "select_environment", lambda *args: None)
    monkeypatch.setitem(sys.modules, "vaws_client_setup", types.SimpleNamespace(
        build_plan=lambda *a: {}, apply_plan=lambda *a: {}, launch_env=lambda *a: {}))
    launches = []
    monkeypatch.setattr(client, "run_client", lambda command, cwd, environment: launches.append((command, cwd, environment)) or 0)
    return client, root, stage, receipt, launches


def test_new_release_runs_its_own_launcher_with_prepared_interpreter(client_fixture, monkeypatch):
    client, root, stage, receipt, launches = client_fixture
    configure(root)
    monkeypatch.setenv("VAWS_ENV_RECEIPT", "old-pin")
    monkeypatch.setenv("VAWS_MANAGED_ENV_RECEIPT", "old-managed-pin")
    monkeypatch.setenv("VAWS_VENV_REEXEC", "old-reexec")
    class ExecObserved(BaseException):
        pass
    calls = []
    def execute(executable, argv, environment):
        calls.append((executable, argv, environment))
        raise ExecObserved
    monkeypatch.setattr(client.os, "execvpe", execute)
    with pytest.raises(ExecObserved):
        client.main(["codex", "--", "literal 中文", "$(not-shell)"])
    executable, argv, environment = calls[0]
    assert executable == receipt["python"]
    assert argv[1] == str(stage / ".agents/scripts/vaws_client.py")
    assert argv[-2:] == ["literal 中文", "$(not-shell)"]
    assert environment["VAWS_RELEASE_LAUNCH"] == "1"
    assert not {"VAWS_ENV_RECEIPT", "VAWS_MANAGED_ENV_RECEIPT", "VAWS_VENV_REEXEC"} & environment.keys()
    assert not launches
    assert json.loads((stage / ".vaws-local/github.json").read_text())["login"] == "alice"
    assert json.loads((stage / ".vaws-local/updates/config.json").read_text())["enabled"] is False


def test_optional_release_exec_failure_continues_original_workspace(client_fixture, monkeypatch, capsys):
    client, root, stage, receipt, launches = client_fixture
    monkeypatch.setattr(client.os, "execvpe", lambda *a: (_ for _ in ()).throw(OSError("fixture exec unavailable")))
    assert client.main(["codex"]) == 0
    assert len(launches) == 1
    assert "fixture exec unavailable" in capsys.readouterr().err


def test_release_handoff_does_not_loop_and_marker_stays_out_of_native_env(client_fixture, monkeypatch):
    client, root, stage, receipt, launches = client_fixture
    monkeypatch.setenv("VAWS_RELEASE_LAUNCH", "1")
    monkeypatch.setattr(client.os, "execvpe", lambda *a: pytest.fail("second release handoff"))
    assert client.main(["codex"]) == 0
    assert "VAWS_RELEASE_LAUNCH" not in client.client_environment(launches[0][2])


def test_prepared_dependency_unavailable_does_not_block_current_client(client_fixture, monkeypatch):
    client, root, stage, receipt, launches = client_fixture
    import vaws_environment as envs
    def ready(source):
        if source == stage:
            raise envs.EnvironmentError("native release environment is not prepared")
        return receipt
    monkeypatch.setattr(envs, "native_ready", ready)
    monkeypatch.setattr(client.os, "execvpe", lambda *a: pytest.fail("unprepared release executable"))
    assert client.main(["codex"]) == 0
    assert len(launches) == 1


def test_identity_copy_keeps_only_setup_fields_and_preserves_target(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    configure(source)
    identity_path = source / ".vaws-local/github.json"
    identity_path.write_text(json.dumps({"schema": "vaws.github.v1", "login": "alice", "github_user_id": 123,
                                         "forks": {"workspace": "alice/vllm-ascend-workspace-1"},
                                         "task_id": "do-not-copy", "token": "do-not-copy"}))
    entry.copy_workspace_identity(source, target)
    copied = target / ".vaws-local/github.json"
    assert json.loads(copied.read_text()) == {"schema": "vaws.github.v1", "login": "alice", "github_user_id": 123,
                                            "forks": {"workspace": "alice/vllm-ascend-workspace-1"}}
    copied.write_text('{"schema":"vaws.github.v1","login":"existing"}')
    entry.copy_workspace_identity(source, target)
    assert json.loads(copied.read_text())["login"] == "existing"
    assert not (target / ".vaws-local/updates/config.json").exists()


def test_fresh_client_copy_gets_snapshot_but_existing_target_is_untouched(client_fixture, monkeypatch):
    client, root, stage, receipt, launches = client_fixture
    configure(root)
    monkeypatch.setenv("VAWS_RELEASE_LAUNCH", "1")
    target = root.parent / "copy"
    monkeypatch.setattr(client, "prepare_workspace", lambda *a, **k: {"state": "ready", "workspace": str(target)})
    assert client.main(["codex"]) == 0
    identity_path = target / ".vaws-local/github.json"
    assert json.loads(identity_path.read_text())["login"] == "alice"
    identity_path.unlink()
    monkeypatch.setattr(client, "prepare_workspace", lambda *a, **k: {"state": "reused", "workspace": str(target)})
    assert client.main(["codex", "--workspace", str(target)]) == 0
    assert not identity_path.exists()


def test_release_handoff_really_executes_prepared_python(tmp_path):
    root, stage = tmp_path / "original", tmp_path / "release 中文"
    root.mkdir()
    launcher = stage / ".agents/scripts/vaws_client.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "import json,os,sys;print(json.dumps({'prefix':sys.prefix,'argv':sys.argv[1:],"
        "'pin':os.environ.get('VAWS_ENV_RECEIPT'),'hop':os.environ.get('VAWS_RELEASE_LAUNCH')}))\n",
        encoding="utf-8")
    environment_root = tmp_path / "prepared environment"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment_root)
    executable = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys;from pathlib import Path\n"
        "sys.path.insert(0,sys.argv[1]);import vaws_client as c\n"
        "import vaws_workspace_entry as e;import vaws_workspace_update as u;import vaws_environment as d\n"
        "c.ROOT=Path(sys.argv[2]);c.resolve_client=lambda name:['unused-native-command']\n"
        "e.report_workspace_entry=lambda *a,**k:None;u.prepared_source=lambda root:Path(sys.argv[3])\n"
        "d.native_ready=lambda root:{'python':sys.argv[4]}\n"
        "raise SystemExit(c.main(['codex','--','literal 中文','$(never-shell)']))\n",
        encoding="utf-8")
    result = subprocess.run([sys.executable, str(driver), str(scripts), str(root), str(stage), str(executable)],
                            env={**os.environ, "VAWS_ENV_RECEIPT": "old-pin", "VAWS_MANAGED_ENV_RECEIPT": "old-owner-pin"},
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert Path(observed["prefix"]).resolve() == environment_root.resolve()
    assert observed["argv"] == ["codex", "--", "literal 中文", "$(never-shell)"]
    assert observed["pin"] is None and observed["hop"] == "1"
