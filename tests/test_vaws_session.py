import json
from pathlib import Path

import yaml

from conftest import run_vaws


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def seed_overlay(vaws_repo, target_name="single-default") -> Path:
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    simulation_root = vaws_repo.parent / "simulation-runtime"
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(
            {
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
        ),
        encoding="utf-8",
    )
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
                        "auth_group": "shared-lab-a",
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


def test_session_create_builds_local_and_remote_manifests_and_worktrees(vaws_repo):
    simulation_root = ensure_target(vaws_repo)

    result = run_vaws(vaws_repo, "session", "create", "feat_x")

    assert result.returncode == 0
    local_manifest = read_yaml(
        vaws_repo / ".workspace.local" / "sessions" / "feat_x" / "manifest.yaml"
    )
    assert local_manifest["name"] == "feat_x"
    assert local_manifest["target"] == "single-default"
    assert local_manifest["workspace_ref"]["branch"] == "feature/feat_x"
    assert local_manifest["workspace_ref"]["base_ref"] == "origin/main"
    assert local_manifest["vllm_ref"]["branch"] == "feature/feat_x"
    assert local_manifest["vllm_ascend_ref"]["branch"] == "feature/feat_x"

    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    remote_manifest = read_yaml(
        runtime_root / ".vaws" / "sessions" / "feat_x" / "manifest.yaml"
    )
    assert remote_manifest["runtime"]["venv_path"].endswith("/feat_x/.venv")
    assert (runtime_root / ".vaws" / "sessions" / "feat_x" / "vllm" / ".git").exists()
    assert (
        runtime_root / ".vaws" / "sessions" / "feat_x" / "vllm-ascend" / ".git"
    ).exists()


def test_session_switch_updates_current_pointer_and_runtime_symlinks(vaws_repo):
    simulation_root = ensure_target(vaws_repo)
    assert run_vaws(vaws_repo, "session", "create", "feat_x").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_y").returncode == 0

    result = run_vaws(vaws_repo, "session", "switch", "feat_y")

    assert result.returncode == 0
    current = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert current["schema_version"] == 1
    assert current["current_session"] == "feat_y"
    assert current["current_target"] == "single-default"
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert (runtime_root / ".vaws" / "current").is_symlink()
    assert (runtime_root / ".vaws" / "current").resolve().name == "feat_y"
    assert (runtime_root / "vllm").is_symlink()
    assert (runtime_root / "vllm").resolve().name == "vllm"
    assert (runtime_root / "vllm-ascend").is_symlink()
    assert (runtime_root / "vllm-ascend").resolve().name == "vllm-ascend"


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
