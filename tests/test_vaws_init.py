import json
import sys
from pathlib import Path
from typing import Optional

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import seed_overlay_files
from tools.lib.config import RepoPaths
from tools.lib.init_flow import InitRequest, run_init
from tools.vaws import build_parser


def _write_auth_overlay(vaws_repo: Path) -> None:
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "ssh_auth": {
                    "refs": {
                        "legacy-server-auth": {
                            "kind": "ssh-key",
                            "username": "root",
                        }
                    }
                },
                "git_auth": {
                    "refs": {
                        "legacy-git-auth": {
                            "kind": "ssh-agent",
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_default_server_auth(
    vaws_repo: Path,
    *,
    kind: str,
    username: str = "root",
    password_env: Optional[str] = None,
    key_path: Optional[str] = None,
) -> None:
    overlay = vaws_repo / ".workspace.local"
    auth = yaml.safe_load((overlay / "auth.yaml").read_text(encoding="utf-8"))
    auth["ssh_auth"]["refs"]["default-server-auth"] = {
        "kind": kind,
        "username": username,
    }
    if password_env is not None:
        auth["ssh_auth"]["refs"]["default-server-auth"]["password_env"] = password_env
    if key_path is not None:
        auth["ssh_auth"]["refs"]["default-server-auth"]["key_path"] = key_path
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(auth, sort_keys=False),
        encoding="utf-8",
    )


def _set_remote_url(repo: Path, relative_path: str, remote_name: str, url: str) -> None:
    import subprocess

    result = subprocess.run(
        ["git", "remote", "set-url", remote_name, url],
        cwd=repo / relative_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _write_ready_repos_overlay(vaws_repo: Path) -> None:
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "workspace": {
                    "path": ".",
                    "default_branch": "main",
                    "protected_branches": ["main"],
                    "push_remote": "origin",
                    "upstream_remote": "upstream",
                },
                "submodules": {
                    "vllm": {
                        "path": "vllm",
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "upstream_url": "https://github.com/vllm-project/vllm.git",
                        "origin_url": "git@github.com:alice/vllm.git",
                    },
                    "vllm-ascend": {
                        "path": "vllm-ascend",
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "upstream_url": "https://github.com/vllm-project/vllm-ascend.git",
                        "origin_url": "git@github.com:alice/vllm-ascend.git",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _set_remote_url(vaws_repo, "vllm", "origin", "git@github.com:alice/vllm.git")
    _set_remote_url(
        vaws_repo,
        "vllm-ascend",
        "origin",
        "git@github.com:alice/vllm-ascend.git",
    )


def _write_ready_server(vaws_repo: Path, *, server_name: str, server_host: str) -> None:
    overlay = vaws_repo / ".workspace.local"
    servers = yaml.safe_load((overlay / "servers.yaml").read_text(encoding="utf-8"))
    servers["servers"][server_name] = {
        "host": server_host,
        "port": 22,
        "login_user": "root",
        "ssh_auth_ref": "default-server-auth",
        "status": "ready",
        "verification": {
            "status": "ready",
            "summary": "runtime already verified",
            "checks": [],
            "runtime": {
                "host_name": server_name,
                "host": server_host,
                "host_port": 22,
                "login_user": "root",
                "transport": "simulation",
                "workspace_root": "/vllm-workspace",
                "ssh_port": 63269,
                "container_name": "vaws-workspace",
                "image_ref": "quay.nju.edu.cn/ascend/vllm-ascend:latest",
                "bootstrap_mode": "host-then-container",
                "container_endpoint": f"simulation://{server_name}/vaws-workspace",
                "host_workspace_path": f"/root/.vaws/targets/{server_name}/workspace",
            },
        },
        "runtime": {
            "image_ref": "quay.nju.edu.cn/ascend/vllm-ascend:latest",
            "container_name": "vaws-workspace",
            "ssh_port": 63269,
            "workspace_root": "/vllm-workspace",
            "bootstrap_mode": "host-then-container",
            "host_workspace_path": f"/root/.vaws/targets/{server_name}/workspace",
        },
    }
    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(servers, sort_keys=False),
        encoding="utf-8",
    )
    state = json.loads((overlay / "state.json").read_text(encoding="utf-8"))
    state["server_verifications"] = {
        server_name: {
            "status": "ready",
            "summary": "runtime already verified",
            "checks": [],
            "runtime": {
                "host_name": server_name,
                "host": server_host,
                "host_port": 22,
                "login_user": "root",
                "transport": "simulation",
                "workspace_root": "/vllm-workspace",
                "ssh_port": 63269,
                "container_name": "vaws-workspace",
                "image_ref": "quay.nju.edu.cn/ascend/vllm-ascend:latest",
                "bootstrap_mode": "host-then-container",
                "container_endpoint": f"simulation://{server_name}/vaws-workspace",
                "host_workspace_path": f"/root/.vaws/targets/{server_name}/workspace",
            },
        }
    }
    (overlay / "state.json").write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def test_build_parser_accepts_init_server_name_and_local_only():
    parser = build_parser()

    args = parser.parse_args(
        ["init", "--server-name", "lab-a", "--local-only"]
    )

    assert args.command == "init"
    assert args.server_name == "lab-a"
    assert args.local_only is True


def test_run_init_remote_first_runs_foundation_git_profile_then_fleet_add(
    vaws_repo, monkeypatch
):
    calls = []

    def fake_foundation(paths):
        calls.append("foundation")
        return 0

    def fake_git_profile(paths, **kwargs):
        calls.append(("git-profile", kwargs))
        return 0

    def fake_ensure_secret_refs(**kwargs):
        calls.append(("secret-refs", kwargs))

    def fake_add_fleet_server(
        paths,
        server_name,
        server_host,
        ssh_auth_ref=None,
        server_user="root",
        server_port=22,
        runtime_image="",
        runtime_container="",
        runtime_ssh_port=63269,
        runtime_workspace_root="",
        runtime_bootstrap_mode="",
    ):
        calls.append(
            (
                "fleet-add",
                {
                    "server_name": server_name,
                    "server_host": server_host,
                    "server_user": server_user,
                    "server_port": server_port,
                    "runtime_image": runtime_image,
                    "runtime_container": runtime_container,
                    "runtime_ssh_port": runtime_ssh_port,
                    "runtime_workspace_root": runtime_workspace_root,
                    "runtime_bootstrap_mode": runtime_bootstrap_mode,
                },
            )
        )
        return 0

    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", fake_foundation)
    monkeypatch.setattr(init_flow, "git_profile", fake_git_profile)
    monkeypatch.setattr(init_flow, "ensure_bootstrap_secret_refs", fake_ensure_secret_refs)
    monkeypatch.setattr(init_flow, "add_fleet_server", fake_add_fleet_server)

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="10.0.0.12",
            server_user="root",
            vllm_origin_url="git@github.com:alice/vllm.git",
            vllm_ascend_origin_url="git@github.com:alice/vllm-ascend.git",
        ),
    )

    assert result == 0
    assert [entry[0] if isinstance(entry, tuple) else entry for entry in calls] == [
        "foundation",
        "git-profile",
        "secret-refs",
        "fleet-add",
    ]
    fleet_call = calls[-1][1]
    assert fleet_call["server_name"] == "10.0.0.12"
    assert fleet_call["server_host"] == "10.0.0.12"

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["requested_mode"] == "remote-first"


def test_run_init_fails_cleanly_when_server_name_resolution_returns_none(
    vaws_repo, monkeypatch, capsys
):
    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "_ensure_overlay", lambda paths: None)
    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(init_flow, "git_profile", lambda paths, **kwargs: 0)
    monkeypatch.setattr(
        init_flow,
        "_ensure_requested_git_auth_secret_refs",
        lambda request: None,
    )
    monkeypatch.setattr(
        init_flow,
        "_preserve_requested_git_auth",
        lambda paths, request: None,
    )
    monkeypatch.setattr(init_flow, "_resolved_server_name", lambda request: None)

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="173.125.1.2",
            vllm_ascend_origin_url="git@github.com:alice/vllm-ascend.git",
        ),
    )
    output = capsys.readouterr().out.lower()

    assert result == 1
    assert "missing server name" in output


def test_run_init_rerun_reuses_existing_topology_and_ready_server(
    vaws_repo, monkeypatch, capsys
):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_default_server_auth(vaws_repo, kind="ssh-key", username="root")
    _write_ready_repos_overlay(vaws_repo)
    _write_ready_server(vaws_repo, server_name="lab-a", server_host="10.0.0.12")

    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(
        init_flow,
        "ensure_bootstrap_secret_refs",
        lambda **kwargs: pytest.fail("secret refs should not run for existing ready server"),
    )
    monkeypatch.setattr(
        init_flow,
        "add_fleet_server",
        lambda *args, **kwargs: pytest.fail("fleet add should not run for existing ready server"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="10.0.0.12",
            server_name="lab-a",
            server_user="root",
        ),
    )

    assert result == 0
    output = capsys.readouterr().out.lower()
    assert "init: ready" in output
    assert "existing" in output

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["requested_mode"] == "remote-first"
    assert state["current_target"] == "lab-a"
    assert state["runtime"]["host_name"] == "lab-a"
    assert state["runtime"]["transport"] == "simulation"


def test_run_init_rerun_reuses_ready_server_for_same_host_without_explicit_server_name(
    vaws_repo, monkeypatch, capsys
):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_default_server_auth(vaws_repo, kind="ssh-key", username="root")
    _write_ready_repos_overlay(vaws_repo)
    _write_ready_server(vaws_repo, server_name="lab-a", server_host="10.0.0.12")

    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(
        init_flow,
        "ensure_bootstrap_secret_refs",
        lambda **kwargs: pytest.fail("secret refs should not run for existing ready server"),
    )
    monkeypatch.setattr(
        init_flow,
        "add_fleet_server",
        lambda *args, **kwargs: pytest.fail("fleet add should not run when a ready host match already exists"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="10.0.0.12",
            server_user="root",
        ),
    )

    assert result == 0
    output = capsys.readouterr().out.lower()
    assert "init: ready" in output
    assert "existing lab-a" in output

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["current_target"] == "lab-a"
    assert state["runtime"]["host_name"] == "lab-a"


def test_run_init_explicit_server_name_does_not_silently_reuse_different_ready_name(
    vaws_repo, monkeypatch
):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_ready_repos_overlay(vaws_repo)
    _write_ready_server(vaws_repo, server_name="lab-a", server_host="10.0.0.12")

    calls = {}

    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(
        init_flow,
        "ensure_bootstrap_secret_refs",
        lambda **kwargs: calls.setdefault("secret_refs", True),
    )

    def fake_add_fleet_server(
        paths,
        server_name,
        server_host,
        ssh_auth_ref=None,
        server_user="root",
        server_port=22,
        runtime_image="",
        runtime_container="",
        runtime_ssh_port=63269,
        runtime_workspace_root="",
        runtime_bootstrap_mode="",
    ):
        calls["fleet_add"] = {
            "server_name": server_name,
            "server_host": server_host,
        }
        return 0

    monkeypatch.setattr(init_flow, "add_fleet_server", fake_add_fleet_server)

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="10.0.0.12",
            server_name="lab-b",
            server_user="root",
        ),
    )

    assert result == 0
    assert calls["fleet_add"]["server_name"] == "lab-b"
    assert calls["fleet_add"]["server_host"] == "10.0.0.12"


def test_run_init_same_host_with_mismatched_runtime_config_does_not_reuse_ready_server(
    vaws_repo, monkeypatch
):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_ready_repos_overlay(vaws_repo)
    _write_ready_server(vaws_repo, server_name="lab-a", server_host="10.0.0.12")

    calls = {}

    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(
        init_flow,
        "ensure_bootstrap_secret_refs",
        lambda **kwargs: calls.setdefault("secret_refs", kwargs),
    )

    def fake_add_fleet_server(
        paths,
        server_name,
        server_host,
        ssh_auth_ref=None,
        server_user="root",
        server_port=22,
        runtime_image="",
        runtime_container="",
        runtime_ssh_port=63269,
        runtime_workspace_root="",
        runtime_bootstrap_mode="",
    ):
        calls["fleet_add"] = {
            "server_name": server_name,
            "server_host": server_host,
            "server_user": server_user,
            "server_port": server_port,
            "runtime_image": runtime_image,
            "runtime_container": runtime_container,
            "runtime_ssh_port": runtime_ssh_port,
            "runtime_workspace_root": runtime_workspace_root,
            "runtime_bootstrap_mode": runtime_bootstrap_mode,
        }
        return 0

    monkeypatch.setattr(init_flow, "add_fleet_server", fake_add_fleet_server)

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="10.0.0.12",
            server_user="root",
            server_port=2201,
            runtime_image="example/runtime:latest",
            runtime_container="workspace-a",
            runtime_ssh_port=63270,
            runtime_workspace_root="/workspace-root",
        ),
    )

    assert result == 0
    assert calls["fleet_add"]["server_name"] == "10.0.0.12"
    assert calls["fleet_add"]["server_host"] == "10.0.0.12"
    assert calls["fleet_add"]["server_port"] == 2201
    assert calls["fleet_add"]["runtime_image"] == "example/runtime:latest"
    assert calls["fleet_add"]["runtime_container"] == "workspace-a"
    assert calls["fleet_add"]["runtime_ssh_port"] == 63270
    assert calls["fleet_add"]["runtime_workspace_root"] == "/workspace-root"


def test_run_init_reuse_updates_requested_server_auth_settings(vaws_repo, monkeypatch):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    _write_default_server_auth(
        vaws_repo,
        kind="ssh-key",
        username="root",
        key_path="/tmp/old-server.key",
    )
    _write_ready_repos_overlay(vaws_repo)
    _write_ready_server(vaws_repo, server_name="lab-a", server_host="10.0.0.12")
    monkeypatch.setenv("SERVER_PASSWORD", "already-staged")

    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(
        init_flow,
        "add_fleet_server",
        lambda *args, **kwargs: pytest.fail("reuse path should not attach runtime"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="10.0.0.12",
            server_name="lab-a",
            server_user="root",
            server_auth_mode="password",
            server_password_env="SERVER_PASSWORD",
        ),
    )

    assert result == 0
    auth = yaml.safe_load((vaws_repo / ".workspace.local" / "auth.yaml").read_text())
    assert auth["ssh_auth"]["refs"]["default-server-auth"]["kind"] == "password"
    assert auth["ssh_auth"]["refs"]["default-server-auth"]["username"] == "root"
    assert auth["ssh_auth"]["refs"]["default-server-auth"]["password_env"] == "SERVER_PASSWORD"
    assert "key_path" not in auth["ssh_auth"]["refs"]["default-server-auth"]


def test_run_init_direct_preserves_requested_git_auth_settings(vaws_repo, monkeypatch):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    monkeypatch.setattr("tools.lib.init_flow.run_foundation", lambda paths: 0)
    monkeypatch.setattr(
        "tools.lib.init_flow.add_fleet_server",
        lambda *args, **kwargs: pytest.fail("local-only direct init should not attach runtime"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            local_only=True,
            vllm_ascend_origin_url="git@github.com:alice/vllm-ascend.git",
            git_auth_mode="ssh-key",
            git_key_path="/tmp/direct-git.key",
        ),
    )

    assert result == 0
    auth = yaml.safe_load((vaws_repo / ".workspace.local" / "auth.yaml").read_text())
    assert auth["git_auth"]["refs"]["default-git-auth"]["kind"] == "ssh-key"
    assert auth["git_auth"]["refs"]["default-git-auth"]["key_path"] == "/tmp/direct-git.key"


def test_run_init_local_only_rejects_unstaged_git_token_before_preserving_auth(
    vaws_repo, monkeypatch, capsys
):
    seed_overlay_files(vaws_repo)
    _write_auth_overlay(vaws_repo)
    monkeypatch.delenv("MISSING_GIT_TOKEN", raising=False)
    monkeypatch.setattr("tools.lib.init_flow.run_foundation", lambda paths: 0)
    monkeypatch.setattr(
        "tools.lib.init_flow.add_fleet_server",
        lambda *args, **kwargs: pytest.fail("local-only token validation failure should not attach runtime"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            local_only=True,
            vllm_ascend_origin_url="git@github.com:alice/vllm-ascend.git",
            git_auth_mode="token",
            git_token_env="MISSING_GIT_TOKEN",
        ),
    )

    assert result == 1
    output = capsys.readouterr().out.lower()
    assert "git token" in output
    assert "missing_git_token" in output

    auth = yaml.safe_load((vaws_repo / ".workspace.local" / "auth.yaml").read_text())
    default_git_auth = auth["git_auth"]["refs"].get("default-git-auth")
    assert default_git_auth is None or default_git_auth.get("token_env") != "MISSING_GIT_TOKEN"


def test_run_init_local_only_records_mode_without_attaching_runtime(
    vaws_repo, monkeypatch
):
    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(init_flow, "git_profile", lambda paths, **kwargs: 0)
    monkeypatch.setattr(
        init_flow,
        "ensure_bootstrap_secret_refs",
        lambda **kwargs: pytest.fail("local-only should not enforce server secret refs"),
    )
    monkeypatch.setattr(
        init_flow,
        "add_fleet_server",
        lambda *args, **kwargs: pytest.fail("local-only should not attach runtime"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="10.0.0.12",
            local_only=True,
            server_user="root",
            vllm_ascend_origin_url="git@github.com:alice/vllm-ascend.git",
        ),
    )

    assert result == 0
    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["requested_mode"] == "local-only"
    assert "current_target" not in state
    assert "runtime" not in state


def test_run_init_local_only_preserves_legacy_success_without_git_profile_input(
    vaws_repo, monkeypatch, capsys
):
    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(
        init_flow,
        "add_fleet_server",
        lambda *args, **kwargs: pytest.fail("local-only compatibility path should not attach runtime"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(local_only=True),
    )

    assert result == 0
    output = capsys.readouterr().out.lower()
    assert "git-profile: needs_input" in output
    assert "init: ready (local-only)" in output

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["lifecycle"]["requested_mode"] == "local-only"


def test_run_init_local_only_rejects_unrelated_git_profile_failure(
    vaws_repo, monkeypatch, capsys
):
    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)

    def fake_git_profile(paths, **kwargs):
        print("git-profile: failed: invalid auth overlay")
        return 1

    monkeypatch.setattr(init_flow, "git_profile", fake_git_profile)
    monkeypatch.setattr(
        init_flow,
        "add_fleet_server",
        lambda *args, **kwargs: pytest.fail("local-only error path should not attach runtime"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(local_only=True),
    )

    assert result == 1
    output = capsys.readouterr().out.lower()
    assert "git-profile: failed" in output
    assert "init: ready" not in output


def test_run_init_rejects_missing_env_backed_server_auth(vaws_repo, monkeypatch, capsys):
    from tools.lib import init_flow

    monkeypatch.setattr(init_flow, "run_foundation", lambda paths: 0)
    monkeypatch.setattr(init_flow, "git_profile", lambda paths, **kwargs: 0)
    monkeypatch.setattr(
        init_flow,
        "add_fleet_server",
        lambda *args, **kwargs: pytest.fail("fleet add should not run when secret validation fails"),
    )

    result = run_init(
        RepoPaths(root=vaws_repo),
        InitRequest(
            server_host="10.0.0.12",
            server_user="root",
            server_auth_mode="password",
            server_password_env="MISSING_SERVER_PASSWORD",
            vllm_ascend_origin_url="git@github.com:alice/vllm-ascend.git",
        ),
    )

    assert result == 1
    output = capsys.readouterr().out.lower()
    assert "server password" in output
    assert "missing_server_password" in output
