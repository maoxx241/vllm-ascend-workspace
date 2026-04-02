import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib import init_flow
from tools.lib.config import RepoPaths
from tools.lib.overlay import ensure_overlay_layout
from tools.lib.runtime import read_state


class _RepoTargets:
    def to_mapping(self):
        return {
            "workspace": {"push_remote": "origin"},
            "submodules": {
                "vllm": {"push_remote": "origin"},
                "vllm-ascend": {"push_remote": "origin"},
            },
        }


def _ready_capability(payload_detail: str) -> dict:
    return {
        "status": "ready",
        "detail": payload_detail,
        "observed_at": "2026-04-01T00:00:00Z",
        "evidence_source": "test",
    }


def test_run_init_remote_first_writes_server_auth_ref_and_calls_add_machine(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    calls = {}

    monkeypatch.setattr(
        init_flow,
        "ensure_git_auth_ready",
        lambda *_args, **_kwargs: _ready_capability("github auth ready"),
    )
    monkeypatch.setattr(
        init_flow,
        "ensure_repo_topology_ready",
        lambda *_args, **_kwargs: _ready_capability("repo topology ready"),
    )
    monkeypatch.setattr(init_flow, "resolve_repo_targets", lambda *_args, **_kwargs: _RepoTargets())

    def fake_ensure_secret_refs(**kwargs):
        calls["secret_refs"] = kwargs

    def fake_add_machine(*args, **kwargs):
        calls["add_machine"] = {"args": args, "kwargs": kwargs}
        return 0

    monkeypatch.setattr(init_flow, "ensure_bootstrap_secret_refs", fake_ensure_secret_refs)
    monkeypatch.setattr(init_flow, "add_machine", fake_add_machine)

    request = init_flow.InitRequest(
        server_host="10.0.0.12",
        server_user="root",
        server_port=22,
        server_auth_mode="password",
        server_password_env="SERVER_PASSWORD",
    )

    assert init_flow.run_init(paths, request) == 0

    auth = yaml.safe_load(paths.local_auth_file.read_text(encoding="utf-8"))
    assert auth["git_auth"]["refs"]["default-github-cli"]["kind"] == "github-cli"
    assert auth["ssh_auth"]["refs"]["default-server-auth"] == {
        "kind": "password",
        "username": "root",
        "password_env": "SERVER_PASSWORD",
    }
    assert calls["secret_refs"] == {
        "server_auth_mode": "password",
        "server_password_env": "SERVER_PASSWORD",
        "server_password_scope": "workspace-init:first-machine-attach",
        "git_auth_mode": "github-cli",
        "git_token_env": None,
    }
    assert calls["add_machine"]["args"][:3] == (paths, "10.0.0.12", "10.0.0.12")
    assert calls["add_machine"]["kwargs"]["ssh_auth_ref"] == "default-server-auth"


def test_run_init_local_only_uses_git_auth_and_repo_topology_helpers(vaws_repo, monkeypatch, capsys):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    calls = []

    monkeypatch.setattr(
        init_flow,
        "ensure_git_auth_ready",
        lambda *_args, **_kwargs: calls.append("git_auth") or _ready_capability("github auth ready"),
    )
    monkeypatch.setattr(
        init_flow,
        "ensure_repo_topology_ready",
        lambda *_args, **_kwargs: calls.append("repo_topology") or _ready_capability("repo topology ready"),
    )
    monkeypatch.setattr(init_flow, "resolve_repo_targets", lambda *_args, **_kwargs: _RepoTargets())
    monkeypatch.setattr(
        init_flow,
        "add_machine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("add_machine should not run")),
    )

    assert init_flow.run_init(paths, init_flow.InitRequest(local_only=True)) == 0

    output = capsys.readouterr().out.lower()
    assert "local-only" in output
    assert calls == ["git_auth", "repo_topology"]
    state = read_state(paths)
    assert state["repo_targets"]["workspace"]["push_remote"] == "origin"
    assert "current_server" not in state


def test_run_init_local_only_fails_when_git_auth_needs_input(vaws_repo, monkeypatch, capsys):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)

    monkeypatch.setattr(
        init_flow,
        "ensure_git_auth_ready",
        lambda *_args, **_kwargs: {
            "status": "needs_input",
            "detail": "gh auth login required",
        },
    )
    monkeypatch.setattr(
        init_flow,
        "ensure_repo_topology_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("repo topology should not run when git auth is blocked")
        ),
    )

    assert init_flow.run_init(paths, init_flow.InitRequest(local_only=True)) == 1
    assert "needs_input" in capsys.readouterr().out


def test_run_init_remote_first_reuses_matching_ready_server(vaws_repo, monkeypatch):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_servers_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "servers": {
                    "lab-a": {
                        "host": "10.0.0.12",
                        "port": 22,
                        "login_user": "root",
                        "ssh_auth_ref": "default-server-auth",
                        "status": "ready",
                        "runtime": {
                            "image_ref": "quay.nju.edu.cn/ascend/vllm-ascend:latest",
                            "container_name": "vaws-workspace",
                            "ssh_port": 63269,
                            "workspace_root": "/vllm-workspace",
                            "bootstrap_mode": "host-then-container",
                            "host_workspace_path": "/root/.vaws/targets/lab-a/workspace",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    paths.local_auth_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "ssh_auth": {
                    "refs": {
                        "default-server-auth": {
                            "kind": "ssh-key",
                            "username": "root",
                        }
                    }
                },
                "git_auth": {"refs": {}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state = {
        "schema_version": 2,
        "servers": {
            "lab-a": {
                "container_access": {"status": "ready"},
            }
        },
    }
    paths.local_state_file.write_text(yaml.safe_dump(state), encoding="utf-8")
    paths.local_state_file.write_text(
        '{"schema_version": 2, "servers": {"lab-a": {"container_access": {"status": "ready"}}}}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        init_flow,
        "ensure_git_auth_ready",
        lambda *_args, **_kwargs: _ready_capability("github auth ready"),
    )
    monkeypatch.setattr(
        init_flow,
        "ensure_repo_topology_ready",
        lambda *_args, **_kwargs: _ready_capability("repo topology ready"),
    )
    monkeypatch.setattr(init_flow, "resolve_repo_targets", lambda *_args, **_kwargs: _RepoTargets())
    monkeypatch.setattr(
        init_flow,
        "add_machine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("add_machine should not run")),
    )
    monkeypatch.setattr(
        init_flow,
        "ensure_bootstrap_secret_refs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("secret refs should not run when auth already matches")
        ),
    )

    request = init_flow.InitRequest(server_host="10.0.0.12", server_name="lab-a")

    assert init_flow.run_init(paths, request) == 0

    state = read_state(paths)
    assert state["current_server"] == "lab-a"
    assert "current_session" not in state


def test_run_init_remote_first_validates_password_secret_handle(vaws_repo, monkeypatch, capsys):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)

    monkeypatch.setattr(
        init_flow,
        "ensure_git_auth_ready",
        lambda *_args, **_kwargs: _ready_capability("github auth ready"),
    )
    monkeypatch.setattr(
        init_flow,
        "ensure_repo_topology_ready",
        lambda *_args, **_kwargs: _ready_capability("repo topology ready"),
    )
    monkeypatch.setattr(init_flow, "resolve_repo_targets", lambda *_args, **_kwargs: _RepoTargets())

    request = init_flow.InitRequest(
        server_host="10.0.0.12",
        server_auth_mode="password",
        server_password_env=None,
    )

    assert init_flow.run_init(paths, request) == 1

    output = capsys.readouterr().out.lower()
    assert "server password" in output
    assert "secret handle" in output
