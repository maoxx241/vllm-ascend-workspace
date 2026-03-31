import json
import subprocess
import shutil
from pathlib import Path

import pytest
import yaml

from conftest import run_vaws


def test_remotes_normalize_prefers_main(vaws_repo):
    result = run_vaws(vaws_repo, "remotes", "normalize")
    assert result.returncode == 0
    assert "origin/main" in result.stdout


def test_remotes_normalize_uses_overlay_workspace_default_branch(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "workspace": {
                    "default_branch": "release",
                    "push_remote": "upstream",
                }
            }
        ),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "remotes", "normalize")

    assert result.returncode == 0
    assert "upstream/release" in result.stdout


@pytest.mark.parametrize(
    ("contents", "expected_message"),
    [
        ("workspace: [1, 2\n", "invalid"),
        ("workspace: [\n", "invalid"),
    ],
)
def test_remotes_normalize_fails_for_corrupted_repos_config(
    vaws_repo, contents, expected_message
):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text(contents, encoding="utf-8")

    result = run_vaws(vaws_repo, "remotes", "normalize")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert expected_message in output
    assert "repos.yaml" in output
    assert "origin/main" not in output


def test_remotes_normalize_fails_for_workspace_list(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text("workspace: []\n", encoding="utf-8")

    result = run_vaws(vaws_repo, "remotes", "normalize")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid" in output
    assert "repos.yaml" in output
    assert "origin/main" not in output


def test_remotes_normalize_fails_for_invalid_default_branch_type(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump({"workspace": {"default_branch": ["main"]}}),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "remotes", "normalize")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid" in output
    assert "repos.yaml" in output
    assert "origin/main" not in output


def test_remotes_normalize_fails_for_invalid_utf8_repos_config(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_bytes(b"\xff")

    result = run_vaws(vaws_repo, "remotes", "normalize")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid" in output
    assert "repos.yaml" in output
    assert "origin/main" not in output


def test_sync_wrapper_calls_python_entrypoint(repo_root):
    sync_text = (repo_root / "sync").read_text(encoding="utf-8")
    setup_text = (repo_root / "setup").read_text(encoding="utf-8")
    normalized_sync = sync_text.replace('"', "")
    normalized_setup = setup_text.replace('"', "")
    assert "tools/vaws.py sync" in normalized_sync
    assert "tools/vaws.py init" in normalized_setup
    assert "cd ${repo_root}" in normalized_sync or 'cd "$repo_root"' in normalized_sync
    assert "cd ${repo_root}" in normalized_setup or 'cd "$repo_root"' in normalized_setup


def test_sync_command_returns_success(vaws_repo):
    result = run_vaws(vaws_repo, "sync")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output


def seed_sync_overlay(vaws_repo):
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
                    "single-default": {
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


def test_sync_start_bootstraps_session_and_switches(vaws_repo):
    simulation_root = seed_sync_overlay(vaws_repo)
    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0

    result = run_vaws(vaws_repo, "sync", "start", "feat_sync")

    assert result.returncode == 0
    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["current_session"] == "feat_sync"
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert (runtime_root / ".vaws" / "current").resolve().name == "feat_sync"


def test_sync_status_reports_current_target_and_session(vaws_repo):
    seed_sync_overlay(vaws_repo)
    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_status").returncode == 0
    assert run_vaws(vaws_repo, "session", "switch", "feat_status").returncode == 0

    result = run_vaws(vaws_repo, "sync", "status")

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "single-default" in output
    assert "feat_status" in output


def test_setup_wrapper_initializes_overlay_from_outside_repo_root(repo_root, tmp_path):
    overlay = repo_root / ".workspace.local"
    backup = tmp_path / "overlay-backup"
    if overlay.exists():
        shutil.copytree(overlay, backup)
        shutil.rmtree(overlay)
    try:
        result = subprocess.run(
            [str(repo_root / "setup")],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        assert (repo_root / ".workspace.local" / "state.json").exists()
        assert not (tmp_path / ".workspace.local").exists()
    finally:
        if overlay.exists():
            shutil.rmtree(overlay)
        if backup.exists():
            shutil.copytree(backup, overlay)


def test_sync_wrapper_runs_from_repo_root_when_called_elsewhere(repo_root, tmp_path):
    overlay = repo_root / ".workspace.local"
    backup = tmp_path / "overlay-backup"
    if overlay.exists():
        shutil.copytree(overlay, backup)
        shutil.rmtree(overlay)
    overlay.mkdir(exist_ok=True)
    try:
        for name in ("targets.yaml", "repos.yaml", "auth.yaml"):
            (overlay / name).write_text("", encoding="utf-8")
        (overlay / "state.json").write_text("{}\n", encoding="utf-8")

        result = subprocess.run(
            [str(repo_root / "sync"), "status"],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        output = (result.stdout + result.stderr).lower()
        assert "current session" in output
        assert not (tmp_path / ".workspace.local").exists()
    finally:
        if overlay.exists():
            shutil.rmtree(overlay)
        if backup.exists():
            shutil.copytree(backup, overlay)


def test_setup_and_sync_wrappers_are_executable(repo_root):
    assert (repo_root / "setup").is_file()
    assert (repo_root / "sync").is_file()
    assert (repo_root / "setup").stat().st_mode & 0o111
    assert (repo_root / "sync").stat().st_mode & 0o111


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
