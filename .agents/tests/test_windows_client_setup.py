"""Execute generated hooks under native Windows shells with literal input."""
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("windows_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.mark.skipif(os.name != "nt", reason="native Windows shell execution")
@pytest.mark.parametrize("shell", ["cmd", "powershell", "pwsh"])
def test_hook_roundtrip_literal_arguments_stdin_and_exit(shell):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    with tempfile.TemporaryDirectory(prefix="hook 中文 '") as temporary:
        script = Path(temporary) / "echo args.py"
        script.write_text(
            "import json, sys\nprint(json.dumps([sys.argv[1:], sys.stdin.read()], ensure_ascii=False))\nsys.exit(7)\n",
            encoding="utf-8",
        )
        arguments = [sys.executable, str(script), "中文 空格", "x&y", "$value", "single'quote", "%PATH%"]
        command = setup.local_hook_command(arguments)
        assert setup.hook_argv(command) == arguments
        invocation = ([executable, "/d", "/s", "/c", command] if shell == "cmd" else
                      [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command + "; exit $LASTEXITCODE"])
        result = subprocess.run(invocation, input='{"message":"输入文本"}', text=True,
                                encoding="utf-8", capture_output=True, timeout=20)
        assert result.returncode == 7, result.stderr
        assert json.loads(result.stdout) == [arguments[2:], '{"message":"输入文本"}']


def test_foreign_encoded_command_is_not_owned():
    assert not setup.owned_hook_command("powershell.exe -EncodedCommand Zg==", "codex", ROOT)


def test_wsl_hooks_share_windows_task_owner_and_keep_literal_paths(monkeypatch):
    monkeypatch.setattr(setup, "managed_python", lambda: "/mnt/d/work/.vaws-local/venvs/win32/Scripts/python.exe")
    monkeypatch.setattr(setup, "ROOT", PurePosixPath("/mnt/d/work"))
    monkeypatch.setattr(setup, "local_hook_command", lambda argv: __import__("shlex").join(argv))
    registry = r"D:\work\.vaws-local\agent-sessions"
    command = setup.hook_command("grok", PurePosixPath("/mnt/d/work/client 中文"), {"VAWS_AGENT_SESSIONS_DIR": registry})
    argv = setup.hook_argv(command)
    assert argv[0].startswith("/mnt/d/")
    assert argv[1] == r"D:\work\.agents\hooks\vaws_session.py"
    assert argv[argv.index("--project") + 1] == "D:\\work\\client 中文"
    assert argv[-1] == registry
    assert setup.managed_path(registry) == registry
    assert setup.hook_path_identity("/mnt/d/work/client 中文") == setup.hook_path_identity("D:\\work\\client 中文")
    with pytest.raises(ValueError, match="mounted Windows drive"):
        setup.managed_path("/opt/private")


def test_kimi_code_uses_native_home_and_discovers_scoped_project_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "kimi"))
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path / "legacy"))
    setup.kimi_home().mkdir()
    (setup.kimi_home() / "mcp.json").write_text('{"mcpServers":{"existing":{}}}')
    project = tmp_path / "project"
    project.mkdir()
    plan = setup.build_plan("kimi", project, task_only=True)
    assert setup.kimi_home() / "config.toml" in plan["files"]
    assert plan["launch_argv"] == ["kimi"]
    assert plan["launch_cwd"] == str(project)
    assert setup.kimi_home() / "mcp.json" not in plan["files"]
    servers = json.loads(plan["files"][project / ".kimi-code/mcp.json"])["mcpServers"]
    assert servers["vaws-task"]["toolTimeoutMs"] == 600000
    assert "type" not in servers["vaws-task"]


def test_generated_task_owner_migrates_without_rewriting_user_provider_or_policy(monkeypatch):
    monkeypatch.setattr(setup,"ROOT",PurePosixPath("/mnt/d/work"))
    existing={"command":"/mnt/d/work/.vaws-local/venvs/linux/bin/python", "args":setup.task_server_args(),
              "env":{"CUSTOM":"kept"}, "enabled_tools":["vaws_execution"]}
    desired={"command":"/mnt/d/work/.vaws-local/venvs/win32/Scripts/python.exe", "args":setup.task_server_args(),"env":{}}
    merged,action=setup.merge_server_entry(existing,desired)
    assert action == "updated-managed"
    assert merged["command"] == desired["command"]
    assert merged["enabled_tools"] == existing["enabled_tools"]
    assert merged["env"]["CUSTOM"] == "kept"
    custom={**existing,"command":"/usr/bin/python3"}
    assert not setup.managed_task_command_change(custom,desired)
    text='[mcp_servers.vaws_task]\ncommand="old"\nargs=["-m","vaws_coordinator","task-server"]\nenabled_tools=["vaws_execution"]\n[mcp_servers.other]\ncommand="untouched"\n'
    value=setup.tomllib.loads(setup.update_toml_server_command(text,"vaws_task",desired["command"]))
    assert value["mcp_servers"]["vaws_task"]["enabled_tools"] == ["vaws_execution"]
    assert value["mcp_servers"]["other"]["command"] == "untouched"


def test_wsl_setup_replaces_native_spelling_and_keeps_one_owned_hook(monkeypatch):
    import shlex
    monkeypatch.setattr(setup, "ROOT", PurePosixPath("/mnt/d/work"))
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", PurePosixPath("/mnt/d/work/.agents/hooks/vaws_session.py"))
    native = {"command": r"D:\work\.vaws-local\venvs\win32\Scripts\python.exe", "args": setup.task_server_args()}
    desired = {"command": "/mnt/d/work/.vaws-local/venvs/win32/Scripts/python.exe", "args": setup.task_server_args()}
    assert setup.merge_server_entry(native, desired)[0]["command"] == desired["command"]
    old_command = shlex.join([native["command"], r"D:\work\.agents\hooks\vaws_session.py", "--client", "codex", "--project", r"D:\work\project"])
    new_command = shlex.join([desired["command"], r"D:\work\.agents\hooks\vaws_session.py", "--client", "codex", "--project", r"D:\work\project"])
    old = [{"hooks": [{"command": old_command, "custom": "keep"}]}]
    wanted = [{"hooks": [{"command": new_command, "timeout": 12}]}]
    merged = setup.merge_hook_event(old, wanted, "codex", PurePosixPath("/mnt/d/work/project"))
    assert merged == [{"hooks": [{"command": new_command, "custom": "keep", "type": "command", "timeout": 12}]}]
    assert setup.merge_hook_event(merged, wanted, "codex", PurePosixPath("/mnt/d/work/project")) == merged


def test_shared_kimi_migrates_generated_servers_and_preserves_custom_values(monkeypatch):
    monkeypatch.setattr(setup, "ROOT", PurePosixPath("/mnt/d/work"))
    existing = {"command": "/mnt/d/work/.vaws-local/venvs/linux/bin/python",
                "args": setup.remote_dev_server_args(),
                "env": {"REMOTE_DEV_STATE_DIR": "/mnt/d/work/.vaws-local/remote-dev-state", "CUSTOM": "keep"},
                "disabledTools": ["remote_run"]}
    desired = {"command": "./.vaws-local/venvs/win32/Scripts/python.exe",
               "args": setup.remote_dev_server_args(),
               "env": {"REMOTE_DEV_STATE_DIR": r"D:\work\.vaws-local\remote-dev-state"}}
    merged, action = setup.merge_server_entry(existing, desired, checkout=PurePosixPath("/mnt/d/work"))
    assert action == "updated-managed"
    assert merged["command"] == desired["command"]
    assert merged["env"] == {**desired["env"], "CUSTOM": "keep"}
    assert merged["disabledTools"] == existing["disabledTools"]
    custom = {**existing, "command": "/usr/local/bin/my-provider", "env": {"REMOTE_DEV_STATE_DIR": "/custom/state", "CUSTOM": "keep"}}
    preserved, action = setup.merge_server_entry(custom, desired, checkout=PurePosixPath("/mnt/d/work"))
    assert preserved["command"] == custom["command"] and preserved["env"] == custom["env"]
    assert action == "preserved"


def test_windows_mcp_env_crosses_wsl_and_preserves_custom_flags():
    result = setup.windows_interop_env({"REMOTE_DEV_STATE_DIR": r"D:\work\state", "CUSTOM": "value", "WSLENV": "EXISTING/p:CUSTOM/w"})
    assert result["CUSTOM"] == "value"
    assert result["WSLENV"] == "EXISTING/p:CUSTOM/w:REMOTE_DEV_STATE_DIR/w"
    text = '[mcp_servers.vaws_task]\ncommand="managed-python"\n[mcp_servers.vaws_task.env]\nCUSTOM="value"\nWSLENV="EXISTING/p"\n'
    existing = setup.tomllib.loads(text)["mcp_servers"]["vaws_task"]
    desired = {"env": setup.windows_interop_env({"VAWS_AGENT_SESSIONS_DIR": r"D:\work\sessions"})}
    updated = setup.fill_toml_server_env(text, "vaws_task", existing, desired)
    env = setup.tomllib.loads(updated)["mcp_servers"]["vaws_task"]["env"]
    assert env["CUSTOM"] == "value" and env["VAWS_AGENT_SESSIONS_DIR"] == r"D:\work\sessions"
    assert env["WSLENV"] == "EXISTING/p:CUSTOM/w:VAWS_AGENT_SESSIONS_DIR/w"
    assert setup.fill_toml_server_env(updated, "vaws_task", {"env": env}, desired) == updated


def test_existing_json_task_alias_keeps_its_custom_registry(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"vaws_task": {
        "command": "custom", "env": {"VAWS_AGENT_SESSIONS_DIR": "/custom/registry"}}}}))
    assert setup.existing_task_env("claude", tmp_path) == {"VAWS_AGENT_SESSIONS_DIR": "/custom/registry"}


@pytest.mark.parametrize("command", [
    "/mnt/d/work/.venv/bin/python", r"D:\work\.venv\Scripts\python.exe",
    "./.venv/bin/python", r".\.venv\Scripts\python.exe",
    "./.vaws-local/venvs/linux/bin/python",
    "./.vaws-local/venvs/win32/Scripts/python.exe",
    "../work/.venv/bin/python",
])
def test_legacy_owner_uses_configuration_project_not_setup_cwd(command, tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", PurePosixPath("/mnt/d/work"))
    monkeypatch.chdir(tmp_path)
    desired = {"command": "/mnt/d/work/.vaws-local/venvs/win32/Scripts/python.exe",
               "args": setup.task_server_args()}
    existing = {"command": command, "args": setup.task_server_args()}
    assert setup.managed_task_command_change(existing, desired, checkout=PurePosixPath("/mnt/d/work"))
    if command.startswith("./") or command.startswith(".\\"):
        assert not setup.managed_task_command_change(existing, desired, checkout=PurePosixPath("/mnt/d/other"))


@pytest.mark.parametrize("customization", ["different-checkout", "custom-provider", "extra-args", "wrapper"])
def test_live_custom_task_launcher_is_preserved(customization, tmp_path, monkeypatch):
    workspace, other = tmp_path / "workspace", tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    interpreter = workspace / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(setup, "ROOT", workspace)
    desired = {"command": str(workspace / ".vaws-local/venvs/win32/Scripts/python.exe"),
               "args": setup.task_server_args()}
    existing = {"command": str(interpreter), "args": setup.task_server_args(), "enabled_tools": ["vaws_execution"]}
    checkout = workspace
    if customization == "different-checkout":
        checkout = other
        relative = other / ".venv/bin/python"
        relative.parent.mkdir(parents=True)
        relative.touch()
        existing["command"] = "./.venv/bin/python"
    elif customization == "custom-provider":
        existing["command"] = str(other / "my-provider")
        (other / "my-provider").touch()
    elif customization == "extra-args":
        existing["args"] = [*setup.task_server_args(), "--custom-option"]
    else:
        existing["args"] = ["-c", "import runpy; runpy.run_module('vaws_coordinator')"]
    merged, action = setup.merge_server_entry(existing, desired, checkout=checkout)
    assert action == "preserved"
    assert merged == existing


@pytest.mark.parametrize("client", ["claude", "cursor", "codex", "grok"])
@pytest.mark.parametrize("relative", [False, True])
def test_setup_migrates_live_legacy_task_entry_and_preserves_configuration(client, relative, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    interpreter = workspace / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    unrelated_cwd = tmp_path / "setup-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setattr(setup, "ROOT", workspace)
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    desired = {"command": str(workspace / ".vaws-local/venvs/win32/Scripts/python.exe"),
               "args": setup.task_server_args(), "env": {"DEFAULT": "added", "WSLENV": "DEFAULT/w"}}
    Path(desired["command"]).parent.mkdir(parents=True)
    Path(desired["command"]).touch()
    monkeypatch.setattr(setup, "desired_mcp_servers", lambda **kwargs: {"vaws-task": desired})
    existing = {"command": "./.venv/bin/python" if relative else str(interpreter),
                "args": setup.task_server_args(), "enabled_tools": ["vaws_execution"],
                "env": {"CUSTOM": "keep", "VAWS_AGENT_SESSIONS_DIR": str(tmp_path / "custom-registry")}}
    if client in {"claude", "cursor"}:
        path = workspace / (".mcp.json" if client == "claude" else ".cursor/mcp.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": {"vaws_task": existing, "other": {"command": "untouched"}}}))
    else:
        path = workspace / ("." + client) / "config.toml"
        path.parent.mkdir()
        path.write_text("# user settings\napproval_policy = 'on-request'\n"
                        + setup.toml_server_body("vaws_task", existing).replace("\n[mcp_servers.vaws_task.env]",
                            '\nenabled_tools = ["vaws_execution"]\n[mcp_servers.vaws_task.env]')
                        + "\n[mcp_servers.other]\ncommand = 'untouched'\n")
    plan = setup.build_plan(client, workspace, task_only=True)
    rendered = plan["files"][path]
    parsed = (json.loads(rendered)["mcpServers"] if client in {"claude", "cursor"}
              else setup.tomllib.loads(rendered)["mcp_servers"])
    migrated = parsed["vaws_task"]
    assert migrated["command"] == desired["command"]
    assert migrated["args"] == existing["args"]
    assert migrated["enabled_tools"] == existing["enabled_tools"]
    assert {key: migrated["env"][key] for key in existing["env"]} == existing["env"]
    assert migrated["env"]["DEFAULT"] == "added"
    assert "CUSTOM/w" in migrated["env"]["WSLENV"]
    assert parsed["other"] == {"command": "untouched"}
    if client in {"codex", "grok"}:
        assert rendered.startswith("# user settings\napproval_policy = 'on-request'\n")
    for output, content in plan["files"].items():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    repeated = setup.build_plan(client, workspace, task_only=True)
    assert repeated["files"].get(path, rendered) == rendered
