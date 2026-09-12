"""Legacy local-venv providers require their exact generated TOML marker."""
from pathlib import PureWindowsPath

import pytest

from client_setup_fixtures import selected_runtime
from test_knowledge_client_setup import setup


def fixture(monkeypatch, tmp_path, name):
    selected_runtime(monkeypatch, setup, tmp_path)
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    args = setup.remote_dev_server_args() if name == "remote-dev" else setup.knowledge_server_args()
    desired = {"command": str(tmp_path / ".vaws-local/env-links" / ("b" * 64) / "bin/python"),
               "args": args, "env": {setup.PIN_ENV: "/selected/ready.json"}}
    if name == "remote-dev":
        desired["env"].update(REMOTE_DEV_DEFAULT_USER="root", REMOTE_DEV_STATE_DIR=str(tmp_path / ".vaws-local/remote-dev-state"))
        old_env = {"REMOTE_DEV_DEFAULT_USER": "root", "REMOTE_DEV_DEFAULT_ROOT": "/vllm-workspace",
                   "REMOTE_DEV_DEFAULT_CWD": "/vllm-workspace", "REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/vaws-ascend-env.sh",
                   "REMOTE_DEV_RESOLVERS": str(tmp_path / ".agents/lib/vaws_remote_dev_plugin.py") + ":setup"}
    else:
        desired["env"]["VAWS_KNOWLEDGE_CONFIG"] = str(tmp_path / ".vaws-local/knowledge/service.json")
        old_env = {"VAWS_KNOWLEDGE_PROJECT_ROOTS": ".agents/knowledge",
                   "VAWS_KNOWLEDGE_CANDIDATE_ROOT": ".vaws-local/knowledge/candidate",
                   "VAWS_KNOWLEDGE_STATE": ".vaws-local/knowledge/instance"}
    existing = {"command": str(tmp_path / ".venv/bin/python"), "args": args,
                "env": {**old_env, "CUSTOM": "keep"}}
    monkeypatch.setattr(setup, "desired_mcp_servers", lambda **_: {name: desired})
    return existing, desired


@pytest.mark.parametrize("client", ["codex", "grok"])
@pytest.mark.parametrize("name", ["remote-dev", "vaws-knowledge"])
def test_generated_venv_provider_migrates_once_and_retains_user_fields(client, name, tmp_path, monkeypatch):
    existing, desired = fixture(monkeypatch, tmp_path, name)
    key = name.replace("-", "_")
    body = setup.toml_server_body(key, existing).replace("\nargs =", '\nstartup_timeout_sec = 31\nargs =')
    text = setup.managed_toml_text('approval_policy = "on-request"\n', name, body)
    text += '\n[mcp_servers.custom]\ncommand = "kept-provider"\n'
    path = tmp_path / ("." + client) / "config.toml"
    path.parent.mkdir()
    path.write_text(text)
    plan = setup.build_plan(client, tmp_path)
    result = plan["files"][path]
    parsed = setup.tomllib.loads(result)
    migrated = parsed["mcp_servers"][key]
    assert migrated["command"] == desired["command"]
    assert migrated["env"] == {**desired["env"], "CUSTOM": "keep"}
    assert migrated["startup_timeout_sec"] == 31
    assert parsed["approval_policy"] == "on-request"
    assert parsed["mcp_servers"]["custom"] == {"command": "kept-provider"}
    assert "# BEGIN VAWS " + name in result
    assert any(note.get("action") == "updated-managed" for note in plan["notes"])
    path.write_text(result)
    assert setup.build_plan(client, tmp_path)["files"].get(path, result) == result


@pytest.mark.parametrize("case", ["no-marker", "foreign-venv", "custom-module", "marker-suffix", "inline-env"])
def test_custom_provider_does_not_acquire_legacy_ownership(case, tmp_path, monkeypatch):
    existing, _ = fixture(monkeypatch, tmp_path, "remote-dev")
    if case == "foreign-venv":
        existing["command"] = str(tmp_path / "unrelated/.venv/bin/python")
    if case == "custom-module":
        existing["args"] = ["-m", "custom_remote_provider"]
    body = setup.toml_server_body("remote_dev", existing)
    if case == "inline-env":
        body = body.split("\n[mcp_servers.remote_dev.env]", 1)[0] + '\nenv = { CUSTOM = "keep" }\n'
    text = body if case == "no-marker" else setup.managed_toml_text("", "remote-dev", body)
    if case == "marker-suffix":
        text = text.replace("# END VAWS remote-dev", "# END VAWS remote-dev-custom")
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text(text)
    assert setup.build_plan("codex", tmp_path)["files"].get(path, text) == text


def test_recognized_legacy_provider_keeps_custom_environment_locations(tmp_path, monkeypatch):
    existing, desired = fixture(monkeypatch, tmp_path, "remote-dev")
    custom = {"REMOTE_DEV_DEFAULT_ROOT": "/user-root", "REMOTE_DEV_DEFAULT_CWD": "/user-cwd",
              "REMOTE_DEV_RUNTIME_ENV_FILE": "/user-runtime.sh", "REMOTE_DEV_RESOLVERS": "user_plugin:setup"}
    existing["env"].update(custom)
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text(setup.managed_toml_text("", "remote-dev", setup.toml_server_body("remote_dev", existing)))
    value = setup.tomllib.loads(setup.build_plan("codex", tmp_path)["files"][path])["mcp_servers"]["remote_dev"]
    assert value["command"] == desired["command"]
    assert all(value["env"][key] == item for key, item in custom.items())


def test_native_windows_legacy_command_matches_only_this_workspace(monkeypatch):
    root = PureWindowsPath(r"D:\work 用户")
    monkeypatch.setattr(setup, "ROOT", root)
    entry = {"command": str(root / ".venv/Scripts/python.exe"), "args": setup.remote_dev_server_args()}
    text = setup.managed_toml_text("", "remote-dev", setup.toml_server_body("remote_dev", entry))
    assert setup.legacy_generated_toml_server(text, "remote-dev", "remote_dev", entry, root)
    entry["command"] = r"D:\other\.venv\Scripts\python.exe"
    text = setup.managed_toml_text("", "remote-dev", setup.toml_server_body("remote_dev", entry))
    assert not setup.legacy_generated_toml_server(text, "remote-dev", "remote_dev", entry, root)
