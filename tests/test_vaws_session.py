import json
from pathlib import Path
import sys

import yaml

from conftest import run_vaws

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib import fleet, session as session_lib
from tools.lib.config import RepoPaths
from tools.lib.remote import (
    CredentialGroup,
    HostSpec,
    RuntimeSpec,
    TargetContext,
    VerificationCheck,
    VerificationResult,
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def seed_overlay(vaws_repo, target_name="single-default", use_modern_auth=False) -> Path:
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    simulation_root = vaws_repo.parent / "simulation-runtime"
    auth_config = (
        {
            "ssh_auth": {
                "refs": {
                    "shared-lab-a": {
                        "kind": "local-simulation",
                        "username": "root",
                        "simulation_root": str(simulation_root),
                    }
                }
            }
        }
        if use_modern_auth
        else {
            "host_auth": {
                "mode": "local-simulation",
                "credential_groups": {
                    "shared-lab-a": {
                        "username": "root",
                        "simulation_root": str(simulation_root),
                    }
                },
            }
        }
    )
    (overlay / "auth.yaml").write_text(yaml.safe_dump(auth_config), encoding="utf-8")
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "workspace": {
                    "default_branch": "main",
                    "push_remote": "origin",
                },
                "submodules": {
                    "vllm": {
                        "default_branch": "main",
                        "push_remote": "origin",
                    },
                    "vllm-ascend": {
                        "default_branch": "main",
                        "push_remote": "origin",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (overlay / "state.json").write_text("{}\n", encoding="utf-8")
    (overlay / "targets.yaml").write_text(
        yaml.safe_dump(
            {
                "hosts": {
                "host-a": {
                    "host": "127.0.0.1",
                    "port": 22,
                    "login_user": "root",
                    **(
                        {"ssh_auth_ref": "shared-lab-a"}
                        if use_modern_auth
                        else {"auth_group": "shared-lab-a"}
                    ),
                }
            },
                "targets": {
                    target_name: {
                        "hosts": ["host-a"],
                        "runtime": {
                            "image_ref": "registry.example.com/ascend/vllm-ascend:test",
                            "container_name": "vaws-owner",
                            "ssh_port": 63269,
                            "workspace_root": "/vllm-workspace",
                            "bootstrap_mode": "host-then-container",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return simulation_root


def ensure_target(vaws_repo, target_name="single-default") -> Path:
    simulation_root = seed_overlay(vaws_repo, target_name=target_name)
    result = run_vaws(vaws_repo, "target", "ensure", target_name)
    assert result.returncode == 0
    return simulation_root


def ensure_fleet_handoff(vaws_repo, server_name="host-b"):
    simulation_root = seed_overlay(vaws_repo)
    overlay = vaws_repo / ".workspace.local"
    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "bootstrap": {
                    "completed": False,
                    "mode": "remote-first",
                },
                "servers": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    auth = yaml.safe_load((overlay / "auth.yaml").read_text(encoding="utf-8"))
    auth["ssh_auth"] = {
        "refs": {
            "shared-lab-a": {
                "kind": "local-simulation",
                "username": "root",
                "simulation_root": str(simulation_root),
            }
        }
    }
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(auth, sort_keys=False),
        encoding="utf-8",
    )
    state = read_json(overlay / "state.json")
    state["lifecycle"] = {
        "foundation": {"status": "degraded"},
        "git_profile": {"status": "ready"},
    }
    (overlay / "state.json").write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        server_name,
        "--server-host",
        "10.0.0.12",
    )
    assert result.returncode == 0
    return simulation_root, server_name


def _write_colliding_server(vaws_repo, server_name="shared-name") -> None:
    overlay = vaws_repo / ".workspace.local"
    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "bootstrap": {
                    "completed": True,
                    "mode": "remote-first",
                },
                "servers": {
                    server_name: {
                        "host": "127.0.0.1",
                        "port": 22,
                        "login_user": "root",
                        "ssh_auth_ref": "shared-lab-a",
                        "status": "ready",
                        "runtime": {
                            "image_ref": "registry.example.com/ascend/vllm-ascend:test",
                            "container_name": "server-owner",
                            "ssh_port": 63270,
                            "workspace_root": "/vllm-workspace",
                            "bootstrap_mode": "host-then-container",
                            "host_workspace_path": f"/root/.vaws/targets/{server_name}/workspace",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _remove_current_target_kind(vaws_repo) -> None:
    overlay = vaws_repo / ".workspace.local"
    state = read_json(overlay / "state.json")
    state.pop("current_target_kind", None)
    (overlay / "state.json").write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def test_session_create_builds_local_and_remote_manifests_and_worktrees(vaws_repo):
    simulation_root, current_target = ensure_fleet_handoff(vaws_repo)

    result = run_vaws(vaws_repo, "session", "create", "feat_x")

    assert result.returncode == 0
    local_manifest = read_yaml(
        vaws_repo / ".workspace.local" / "sessions" / "feat_x" / "manifest.yaml"
    )
    assert local_manifest["name"] == "feat_x"
    assert local_manifest["target"] == current_target
    assert local_manifest["workspace_ref"]["branch"] == "feature/feat_x"
    assert local_manifest["workspace_ref"]["base_ref"] == "origin/main"
    assert local_manifest["vllm_ref"]["branch"] == "feature/feat_x"
    assert local_manifest["vllm_ascend_ref"]["branch"] == "feature/feat_x"

    runtime_root = simulation_root / current_target / "vllm-workspace"
    remote_manifest = read_yaml(
        runtime_root / ".vaws" / "sessions" / "feat_x" / "manifest.yaml"
    )
    assert remote_manifest["runtime"]["venv_path"].endswith("/feat_x/.venv")
    assert (runtime_root / ".vaws" / "sessions" / "feat_x" / "vllm" / ".git").exists()
    assert (
        runtime_root / ".vaws" / "sessions" / "feat_x" / "vllm-ascend" / ".git"
    ).exists()


def test_session_create_and_switch_use_legacy_target_handoff_kind_when_name_collides(
    vaws_repo,
    monkeypatch,
):
    seed_overlay(vaws_repo, target_name="shared-name")
    _write_colliding_server(vaws_repo, "shared-name")
    assert run_vaws(vaws_repo, "target", "ensure", "shared-name").returncode == 0

    calls = {"target": [], "server": [], "create": [], "switch": []}

    def fake_resolve_server_context(paths, server_name):
        calls["server"].append(server_name)
        raise AssertionError("server resolver should not be used for legacy target handoff")

    def fake_resolve_target_context(paths, target_name):
        calls["target"].append(target_name)
        return TargetContext(
            name=target_name,
            host=HostSpec(
                name="host-a",
                host="127.0.0.1",
                port=22,
                login_user="root",
                auth_group="shared-lab-a",
            ),
            credential=CredentialGroup(mode="local-simulation", username="root"),
            runtime=RuntimeSpec(
                image_ref="registry.example.com/ascend/vllm-ascend:test",
                container_name="legacy-owner",
                ssh_port=63269,
                workspace_root="/vllm-workspace",
                bootstrap_mode="host-then-container",
                host_workspace_path=f"/root/.vaws/targets/{target_name}/workspace",
                docker_run_args=[],
            ),
        )

    def fake_create_remote_session(paths, ctx, manifest, transport):
        calls["create"].append((ctx.name, transport, manifest["target"]))

    def fake_switch_remote_session(ctx, session_name, transport):
        calls["switch"].append((ctx.name, transport, session_name))

    monkeypatch.setattr(session_lib, "resolve_server_context", fake_resolve_server_context)
    monkeypatch.setattr(session_lib, "resolve_target_context", fake_resolve_target_context)
    monkeypatch.setattr(session_lib, "create_remote_session", fake_create_remote_session)
    monkeypatch.setattr(session_lib, "switch_remote_session", fake_switch_remote_session)

    paths = RepoPaths(root=vaws_repo)
    assert session_lib.create_session(paths, "feat_collision") == 0
    assert session_lib.switch_session(paths, "feat_collision") == 0

    assert calls["server"] == []
    assert calls["target"] == ["shared-name", "shared-name"]
    assert calls["create"] == [("shared-name", "simulation", "shared-name")]
    assert calls["switch"] == [("shared-name", "simulation", "feat_collision")]


def test_session_create_and_switch_infer_missing_current_target_kind_for_modern_auth_collision(
    vaws_repo,
):
    simulation_root = seed_overlay(
        vaws_repo,
        target_name="shared-name",
        use_modern_auth=True,
    )
    _write_colliding_server(vaws_repo, "shared-name")
    assert run_vaws(vaws_repo, "target", "ensure", "shared-name").returncode == 0
    _remove_current_target_kind(vaws_repo)

    result = run_vaws(vaws_repo, "session", "create", "feat_inferred")
    assert result.returncode == 0
    result = run_vaws(vaws_repo, "session", "switch", "feat_inferred")
    assert result.returncode == 0

    target_runtime_root = simulation_root / "host-a" / "vllm-workspace"
    server_runtime_root = simulation_root / "shared-name" / "vllm-workspace"
    assert (
        target_runtime_root / ".vaws" / "sessions" / "feat_inferred" / "manifest.yaml"
    ).exists()
    assert (
        target_runtime_root / ".vaws" / "current"
    ).resolve() == target_runtime_root / ".vaws" / "sessions" / "feat_inferred"
    assert not (
        server_runtime_root / ".vaws" / "sessions" / "feat_inferred" / "manifest.yaml"
    ).exists()
    state = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert state["current_session"] == "feat_inferred"


def test_session_switch_updates_current_pointer_and_runtime_symlinks(vaws_repo):
    simulation_root, current_target = ensure_fleet_handoff(vaws_repo)
    assert run_vaws(vaws_repo, "session", "create", "feat_x").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_y").returncode == 0

    result = run_vaws(vaws_repo, "session", "switch", "feat_y")

    assert result.returncode == 0
    current = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert current["schema_version"] == 1
    assert current["current_session"] == "feat_y"
    assert current["current_target"] == current_target
    runtime_root = simulation_root / current_target / "vllm-workspace"
    assert (runtime_root / ".vaws" / "current").is_symlink()
    assert (runtime_root / ".vaws" / "current").resolve().name == "feat_y"
    assert (runtime_root / "vllm").is_symlink()
    assert (runtime_root / "vllm").resolve().name == "vllm"
    assert (runtime_root / "vllm-ascend").is_symlink()
    assert (runtime_root / "vllm-ascend").resolve().name == "vllm-ascend"


def test_session_switch_clears_stale_current_session_after_fleet_handoff(
    vaws_repo,
    monkeypatch,
):
    simulation_root, _current_target = ensure_fleet_handoff(vaws_repo)
    assert run_vaws(vaws_repo, "session", "create", "feat_x").returncode == 0
    assert run_vaws(vaws_repo, "session", "switch", "feat_x").returncode == 0

    overlay = vaws_repo / ".workspace.local"
    servers = read_yaml(overlay / "servers.yaml")
    servers["servers"]["host-c"] = {
        "host": "10.0.0.13",
        "port": 22,
        "login_user": "root",
        "ssh_auth_ref": "shared-lab-a",
        "status": "ready",
        "runtime": {
            "image_ref": "registry.example.com/ascend/vllm-ascend:test",
            "container_name": "vaws-owner",
            "ssh_port": 63271,
            "workspace_root": "/vllm-workspace",
            "bootstrap_mode": "host-then-container",
            "host_workspace_path": "/root/.vaws/targets/host-c/workspace",
        },
    }
    overlay.joinpath("servers.yaml").write_text(
        yaml.safe_dump(servers, sort_keys=False),
        encoding="utf-8",
    )

    def fake_verify_runtime(paths, ctx):
        return VerificationResult.ready(
            summary="runtime verified",
            runtime={
                "host_name": ctx.name,
                "host": ctx.host.host,
                "host_port": ctx.host.port,
                "login_user": ctx.host.login_user,
                "transport": "simulation",
                "container_endpoint": f"simulation://{ctx.name}/vllm-workspace",
                "workspace_root": ctx.runtime.workspace_root,
                "ssh_port": ctx.runtime.ssh_port,
                "container_name": ctx.runtime.container_name,
                "image_ref": ctx.runtime.image_ref,
                "bootstrap_mode": ctx.runtime.bootstrap_mode,
                "host_workspace_path": ctx.runtime.host_workspace_path,
            },
            checks=[
                VerificationCheck(
                    name="runtime_state",
                    status="ready",
                    detail="runtime state available",
                )
            ],
        )

    monkeypatch.setattr(fleet, "verify_runtime", fake_verify_runtime)

    assert fleet.verify_fleet_server(RepoPaths(root=vaws_repo), "host-c") == 0

    state = read_json(overlay / "state.json")
    assert state["current_target"] == "host-c"
    assert "current_session" not in state
    assert state["runtime"]["host_name"] == "host-c"


def test_session_create_reports_fleet_handoff_precondition_when_current_target_is_missing(
    vaws_repo,
):
    seed_overlay(vaws_repo)

    result = run_vaws(vaws_repo, "session", "create", "feat_x")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "current target" in output
    assert "fleet" in output
    assert "target ensure" not in output


def test_session_create_surfaces_server_auth_config_errors_after_fleet_handoff(
    vaws_repo,
):
    ensure_fleet_handoff(vaws_repo)
    overlay = vaws_repo / ".workspace.local"
    auth = read_yaml(overlay / "auth.yaml")
    auth.pop("ssh_auth", None)
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(auth, sort_keys=False),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "session", "create", "feat_x")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "missing ssh_auth.refs map" in output
    assert "unknown target" not in output


def test_session_switch_fails_cleanly_when_state_schema_version_is_unsupported(vaws_repo):
    simulation_root = ensure_target(vaws_repo)
    assert run_vaws(vaws_repo, "session", "create", "feat_x").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_y").returncode == 0

    state_path = vaws_repo / ".workspace.local" / "state.json"
    state = read_json(state_path)
    state["schema_version"] = 2
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    result = run_vaws(vaws_repo, "session", "switch", "feat_y")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "schema_version" in output
    assert "unsupported" in output or "invalid" in output
    assert "traceback" not in output
    current = read_json(state_path)
    assert current["schema_version"] == 2
    assert "current_session" not in current


def test_session_switch_fails_when_missing(vaws_repo):
    ensure_target(vaws_repo)
    result = run_vaws(vaws_repo, "session", "switch", "missing")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "unknown" in output
    assert "session" in output


def test_session_status_prints_none_when_unset(vaws_repo):
    ensure_target(vaws_repo)
    result = run_vaws(vaws_repo, "session", "status")
    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "none" in output


def test_session_create_rejects_unsafe_session_name(vaws_repo):
    ensure_target(vaws_repo)
    result = run_vaws(vaws_repo, "session", "create", "../evil")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid session name" in output
    assert not (
        vaws_repo / ".workspace.local" / "sessions" / "evil" / "manifest.yaml"
    ).exists()


def test_session_create_rejects_dot_session_name(vaws_repo):
    ensure_target(vaws_repo)
    result = run_vaws(vaws_repo, "session", "create", ".")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid session name" in output


def test_session_create_fails_when_overlay_state_is_uninitialized(vaws_repo):
    result = run_vaws(vaws_repo, "session", "create", "feat_x")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert ".workspace.local" in output
    assert not (
        vaws_repo / ".workspace.local" / "sessions" / "feat_x" / "manifest.yaml"
    ).exists()


def test_session_create_uses_workspace_default_branch_and_push_remote(vaws_repo):
    ensure_target(vaws_repo)
    repos_path = vaws_repo / ".workspace.local" / "repos.yaml"
    repos_path.write_text(
        yaml.safe_dump(
            {
                "workspace": {
                    "default_branch": "release",
                    "push_remote": "upstream",
                },
                "submodules": {
                    "vllm": {
                        "default_branch": "release",
                        "push_remote": "upstream",
                    },
                    "vllm-ascend": {
                        "default_branch": "release",
                        "push_remote": "upstream",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "session", "create", "feat_release")

    assert result.returncode == 0
    manifest = read_yaml(
        vaws_repo / ".workspace.local" / "sessions" / "feat_release" / "manifest.yaml"
    )
    assert manifest["workspace_ref"]["base_ref"] == "upstream/release"
    assert manifest["vllm_ref"]["base_ref"] == "upstream/release"
    assert manifest["vllm_ascend_ref"]["base_ref"] == "upstream/release"


def test_session_create_fails_for_corrupted_repos_config(vaws_repo):
    ensure_target(vaws_repo)
    repos_path = vaws_repo / ".workspace.local" / "repos.yaml"
    repos_path.write_text("workspace: [1, 2\n", encoding="utf-8")

    result = run_vaws(vaws_repo, "session", "create", "feat_release")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid" in output
    assert "repos.yaml" in output
    assert not (
        vaws_repo / ".workspace.local" / "sessions" / "feat_release" / "manifest.yaml"
    ).exists()


def test_session_create_fails_for_workspace_list(vaws_repo):
    ensure_target(vaws_repo)
    repos_path = vaws_repo / ".workspace.local" / "repos.yaml"
    repos_path.write_text("workspace: []\n", encoding="utf-8")

    result = run_vaws(vaws_repo, "session", "create", "feat_release")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid" in output
    assert "repos.yaml" in output
    assert not (
        vaws_repo / ".workspace.local" / "sessions" / "feat_release" / "manifest.yaml"
    ).exists()


def test_session_create_fails_for_invalid_default_branch_type(vaws_repo):
    ensure_target(vaws_repo)
    repos_path = vaws_repo / ".workspace.local" / "repos.yaml"
    repos_path.write_text(
        yaml.safe_dump({"workspace": {"default_branch": ["main"]}}),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "session", "create", "feat_release")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid" in output
    assert "repos.yaml" in output
    assert not (
        vaws_repo / ".workspace.local" / "sessions" / "feat_release" / "manifest.yaml"
    ).exists()


def test_session_switch_fails_when_overlay_state_is_missing(vaws_repo):
    ensure_target(vaws_repo)
    assert run_vaws(vaws_repo, "session", "create", "feat_x").returncode == 0
    state_path = vaws_repo / ".workspace.local" / "state.json"
    state_path.unlink()

    result = run_vaws(vaws_repo, "session", "switch", "feat_x")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert ".workspace.local" in output
    assert not state_path.exists()


def test_session_switch_fails_with_bootstrap_message_when_overlay_missing(vaws_repo):
    result = run_vaws(vaws_repo, "session", "switch", "feat_x")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert ".workspace.local" in output
    assert "unknown session" not in output


def test_session_status_fails_when_overlay_state_is_missing(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()

    result = run_vaws(vaws_repo, "session", "status")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert ".workspace.local" in output
    assert not (overlay / "state.json").exists()
