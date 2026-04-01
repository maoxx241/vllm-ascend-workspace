import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import run_vaws
from tools.lib import bootstrap
from tools.lib import preflight
from tools.lib.config import RepoPaths
from tools.lib.remote import (
    CredentialGroup,
    HostSpec,
    RuntimeSpec,
    TargetContext,
    _ssh_base_command,
)
from tools.vaws import build_parser


def _remote_url(repo, relative_path, remote_name):
    result = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=repo / relative_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_bootstrap_request_from_args_preserves_legacy_surface():
    parser = build_parser()

    args = parser.parse_args(
        [
            "init",
            "--bootstrap",
            "--server-host",
            "173.125.1.2",
            "--server-port",
            "2201",
            "--server-user",
            "root",
            "--server-auth-mode",
            "password",
            "--server-password-env",
            "SERVER_PASSWORD",
            "--target-name",
            "single-default",
            "--host-name",
            "host-a",
            "--runtime-image",
            "example/runtime:latest",
            "--runtime-container",
            "workspace-a",
            "--runtime-ssh-port",
            "63270",
            "--runtime-workspace-root",
            "/workspace-root",
            "--vllm-origin-url",
            "git@github.com:alice/vllm.git",
            "--vllm-ascend-origin-url",
            "git@github.com:alice/vllm-ascend.git",
            "--git-auth-mode",
            "ssh-agent",
        ]
    )

    request = bootstrap.bootstrap_request_from_args(args)

    assert request.server_host == "173.125.1.2"
    assert request.server_port == 2201
    assert request.server_user == "root"
    assert request.server_auth_mode == "password"
    assert request.server_password_env == "SERVER_PASSWORD"
    assert request.target_name == "single-default"
    assert request.host_name == "host-a"
    assert request.runtime_image == "example/runtime:latest"
    assert request.runtime_container == "workspace-a"
    assert request.runtime_ssh_port == 63270
    assert request.runtime_workspace_root == "/workspace-root"
    assert request.vllm_origin_url == "git@github.com:alice/vllm.git"
    assert request.vllm_ascend_origin_url == "git@github.com:alice/vllm-ascend.git"
    assert request.git_auth_mode == "ssh-agent"


def test_bootstrap_init_routes_compat_request_into_staged_init(vaws_repo, monkeypatch):
    captured = {}

    def fail_legacy_bootstrap_path():
        pytest.fail("bootstrap compatibility path should delegate into staged init")

    def fake_run_staged_init(paths, request):
        captured["paths"] = paths
        captured["request"] = request
        return 17

    monkeypatch.setattr(bootstrap, "_ensure_overlay", lambda *_args, **_kwargs: fail_legacy_bootstrap_path())
    monkeypatch.setattr(bootstrap, "_run_staged_init", fake_run_staged_init, raising=False)

    result = bootstrap.bootstrap_init(
        RepoPaths(root=vaws_repo),
        bootstrap.BootstrapRequest(
            server_host="173.125.1.2",
            server_user="root",
            server_port=2201,
            server_auth_mode="password",
            server_password_env="SERVER_PASSWORD",
            server_key_path="/tmp/server.key",
            target_name="single-default",
            host_name="host-a",
            runtime_image="example/runtime:latest",
            runtime_container="workspace-a",
            runtime_ssh_port=63270,
            runtime_workspace_root="/workspace-root",
            vllm_origin_url="git@github.com:alice/vllm.git",
            vllm_ascend_origin_url="git@github.com:alice/vllm-ascend.git",
            git_auth_mode="ssh-key",
            git_key_path="/tmp/git.key",
            git_token_env="GIT_TOKEN",
        ),
    )

    assert result == 17
    assert captured["paths"].root == vaws_repo
    assert captured["request"].server_host == "173.125.1.2"
    assert captured["request"].server_name == "host-a"
    assert captured["request"].local_only is False
    assert captured["request"].server_user == "root"
    assert captured["request"].server_port == 2201
    assert captured["request"].server_auth_mode == "password"
    assert captured["request"].server_password_env == "SERVER_PASSWORD"
    assert captured["request"].server_key_path == "/tmp/server.key"
    assert captured["request"].runtime_image == "example/runtime:latest"
    assert captured["request"].runtime_container == "workspace-a"
    assert captured["request"].runtime_ssh_port == 63270
    assert captured["request"].runtime_workspace_root == "/workspace-root"
    assert captured["request"].vllm_origin_url == "git@github.com:alice/vllm.git"
    assert (
        captured["request"].vllm_ascend_origin_url
        == "git@github.com:alice/vllm-ascend.git"
    )
    assert captured["request"].git_auth_mode == "ssh-key"
    assert captured["request"].git_key_path == "/tmp/git.key"
    assert captured["request"].git_token_env == "GIT_TOKEN"


def test_bootstrap_init_does_not_swallow_unexpected_runtime_error(vaws_repo, monkeypatch):
    def fake_run_staged_init(paths, request):
        raise RuntimeError("unexpected staged init bug")

    monkeypatch.setattr(bootstrap, "_run_staged_init", fake_run_staged_init, raising=False)

    with pytest.raises(RuntimeError, match="unexpected staged init bug"):
        bootstrap.bootstrap_init(
            RepoPaths(root=vaws_repo),
            bootstrap.BootstrapRequest(
                server_host="173.125.1.2",
                server_user="root",
                vllm_ascend_origin_url="git@github.com:alice/vllm-ascend.git",
            ),
        )


def test_init_bootstrap_local_only_runs_staged_init_flow(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "foundation:" in output
    assert "git-profile: ready" in output
    assert "init: ready (local-only)" in output

    repos = yaml.safe_load((vaws_repo / ".workspace.local" / "repos.yaml").read_text())
    assert "origin_url" not in repos["submodules"]["vllm"]
    assert (
        repos["submodules"]["vllm-ascend"]["origin_url"]
        == "git@github.com:alice/vllm-ascend.git"
    )
    assert _remote_url(vaws_repo, "vllm", "origin") == "https://github.com/vllm-project/vllm.git"
    assert _remote_url(vaws_repo, "vllm", "upstream") == "https://github.com/vllm-project/vllm.git"

    auth = yaml.safe_load((vaws_repo / ".workspace.local" / "auth.yaml").read_text())
    assert auth["ssh_auth"]["refs"] == {}
    assert auth["git_auth"]["refs"]["default-git-auth"]["kind"] == "ssh-key"

    servers = yaml.safe_load((vaws_repo / ".workspace.local" / "servers.yaml").read_text())
    assert servers["servers"] == {}
    assert "bootstrap" not in servers

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["schema_version"] == 1
    assert state["lifecycle"]["requested_mode"] == "local-only"
    assert state["lifecycle"]["git_profile"]["status"] == "ready"
    assert "bootstrap" not in state
    assert "current_target" not in state
    assert "runtime" not in state


def test_init_bootstrap_preserves_requested_git_auth_settings(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
        "--git-auth-mode",
        "ssh-key",
        "--git-key-path",
        "/tmp/bootstrap-git.key",
    )

    assert result.returncode == 0

    auth = yaml.safe_load((vaws_repo / ".workspace.local" / "auth.yaml").read_text())
    assert auth["git_auth"]["refs"]["default-git-auth"]["kind"] == "ssh-key"
    assert auth["git_auth"]["refs"]["default-git-auth"]["key_path"] == "/tmp/bootstrap-git.key"


def test_init_bootstrap_preserves_default_requested_git_auth_mode(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 0

    auth = yaml.safe_load((vaws_repo / ".workspace.local" / "auth.yaml").read_text())
    assert auth["git_auth"]["refs"]["default-git-auth"]["kind"] == "ssh-key"
    assert "key_path" not in auth["git_auth"]["refs"]["default-git-auth"]
    assert "token_env" not in auth["git_auth"]["refs"]["default-git-auth"]


def test_init_bootstrap_local_only_overlay_stays_doctor_compatible(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 0

    doctor_result = run_vaws(vaws_repo, "doctor")

    assert doctor_result.returncode == 0
    assert "doctor: ok" in doctor_result.stdout.lower()


def test_preflight_reports_missing_and_optional_tools(monkeypatch):
    def fake_which(command):
        if command == "gh":
            return None
        return f"/usr/bin/{command}"

    monkeypatch.setattr(preflight.shutil, "which", fake_which)

    report = preflight.check_local_control_plane_deps()

    assert report.status == "degraded"
    assert report.installed_required == ("git", "ssh", "python3")
    assert report.missing_required == ()
    assert report.installed_recommended == ()
    assert report.missing_recommended == ("gh",)


def test_preflight_reports_blocked_when_required_tool_missing(monkeypatch):
    def fake_which(command):
        if command == "ssh":
            return None
        return f"/usr/bin/{command}"

    monkeypatch.setattr(preflight.shutil, "which", fake_which)

    report = preflight.check_local_control_plane_deps()

    assert report.status == "blocked"
    assert report.installed_required == ("git", "python3")
    assert report.missing_required == ("ssh",)


def test_preflight_treats_running_python_as_installed_python3(monkeypatch):
    def fake_which(command):
        if command == "python3":
            return None
        return f"/usr/bin/{command}"

    monkeypatch.setattr(preflight.shutil, "which", fake_which)
    monkeypatch.setattr(preflight.sys, "executable", "/opt/control-plane/bin/python")

    report = preflight.check_local_control_plane_deps()

    assert report.status == "ready"
    assert "python3" in report.installed_required
    assert report.missing_required == ()


def test_ssh_base_command_honors_server_key_path():
    ctx = TargetContext(
        name="single-default",
        host=HostSpec(
            name="host-a",
            host="173.125.1.2",
            port=22,
            login_user="root",
            auth_group="default",
        ),
        credential=CredentialGroup(
            mode="ssh-key",
            username="root",
            key_path="/tmp/control-plane.key",
        ),
        runtime=RuntimeSpec(
            image_ref="image",
            container_name="container",
            ssh_port=63269,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/tmp/workspace",
            docker_run_args=[],
        ),
    )

    command = _ssh_base_command(ctx)

    assert "-i" in command
    assert "/tmp/control-plane.key" in command


def test_init_bootstrap_fails_cleanly_for_malformed_state_json(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "state.json").write_text("not valid json", encoding="utf-8")

    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "state.json" in output
    assert "invalid runtime state" in output or "invalid" in output
    assert "traceback" not in output


def test_init_bootstrap_fails_cleanly_for_unsupported_state_schema_version(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "servers.yaml").write_text("version: 1\nservers: {}\n", encoding="utf-8")
    (overlay / "auth.yaml").write_text(
        "version: 1\nssh_auth: {refs: {}}\ngit_auth: {refs: {}}\n",
        encoding="utf-8",
    )
    (overlay / "repos.yaml").write_text(
        "version: 1\nworkspace: {}\nsubmodules: {}\n",
        encoding="utf-8",
    )
    (overlay / "state.json").write_text('{"schema_version": 2}\n', encoding="utf-8")

    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "schema_version" in output
    assert "unsupported" in output or "invalid" in output
    assert "traceback" not in output
    servers = yaml.safe_load((vaws_repo / ".workspace.local" / "servers.yaml").read_text())
    assert servers["servers"] == {}
    assert "bootstrap" not in servers


def test_init_bootstrap_requires_vllm_ascend_origin_url(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "vllm-ascend" in output
    assert "origin" in output


def test_init_bootstrap_rejects_missing_pre_staged_password_handle(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--server-auth-mode",
        "password",
        "--server-password-env",
        "SERVER_PASSWORD",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "pre-stage" in output
    assert "server_password" in output


def test_init_bootstrap_rejects_invalid_secret_handle_name(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--server-auth-mode",
        "password",
        "--server-password-env",
        "bad-name",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid secret handle" in output
