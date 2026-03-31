import json
import subprocess
import shutil
import stat
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import run_vaws
from tools.lib.remote import (
    CredentialGroup,
    HostSpec,
    RuntimeSpec,
    TargetContext,
    destroy_runtime,
)

COMMUNITY_VLLM_URL = "https://github.com/vllm-project/vllm.git"
COMMUNITY_VLLM_ASCEND_URL = "https://github.com/vllm-project/vllm-ascend.git"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _remote_url(repo: Path, relative_path: str, remote_name: str) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=repo / relative_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def seed_reset_workspace(vaws_repo: Path, target_name: str = "single-default") -> Path:
    simulation_root = vaws_repo.parent / "simulation-runtime"
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)

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
                    "upstream_remote": "upstream",
                },
                "submodules": {
                    "vllm": {
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "origin_url": "git@github.com:alice/vllm.git",
                        "upstream_url": "https://github.com/vllm-project/vllm.git",
                    },
                    "vllm-ascend": {
                        "default_branch": "main",
                        "push_remote": "origin",
                        "upstream_remote": "upstream",
                        "origin_url": "git@github.com:alice/vllm-ascend.git",
                        "upstream_url": "https://github.com/vllm-project/vllm-ascend.git",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
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
    (overlay / "state.json").write_text("{}\n", encoding="utf-8")

    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:alice/vllm.git"],
        cwd=vaws_repo / "vllm",
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "upstream", "https://github.com/vllm-project/vllm.git"],
        cwd=vaws_repo / "vllm",
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:alice/vllm-ascend.git"],
        cwd=vaws_repo / "vllm-ascend",
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "remote",
            "set-url",
            "upstream",
            "https://github.com/vllm-project/vllm-ascend.git",
        ],
        cwd=vaws_repo / "vllm-ascend",
        check=True,
        text=True,
        capture_output=True,
    )

    return simulation_root


def add_competing_target(vaws_repo: Path, target_name: str = "single-backup") -> None:
    targets_path = vaws_repo / ".workspace.local" / "targets.yaml"
    targets_config = yaml.safe_load(targets_path.read_text(encoding="utf-8"))
    targets_config["targets"][target_name] = {
        "hosts": ["host-a"],
        "runtime": {
            "image_ref": "registry.example.com/ascend/vllm-ascend:test",
            "container_name": "vaws-backup",
            "ssh_port": 63270,
            "workspace_root": "/vllm-workspace-backup",
            "bootstrap_mode": "host-then-container",
        },
    }
    targets_path.write_text(yaml.safe_dump(targets_config), encoding="utf-8")


def prepare_reset_confirmation(vaws_repo: Path):
    overlay = vaws_repo / ".workspace.local"
    result = run_vaws(vaws_repo, "reset", "--prepare")
    assert result.returncode == 0

    output = result.stdout + result.stderr
    lowered = output.lower()
    assert "wipe local workspace identity" in lowered
    assert "remote runtime context" in lowered
    assert "overlay" in lowered
    assert "I authorize wiping local workspace identity and remote runtime" in output

    request_path = overlay / "reset-request.json"
    assert request_path.exists()
    request_data = json.loads(request_path.read_text(encoding="utf-8"))
    confirmation_id = request_data.get("confirmation_id")
    assert isinstance(confirmation_id, str) and confirmation_id.strip()
    return confirmation_id, output


def seed_initialized_reset_state(vaws_repo: Path) -> Path:
    simulation_root = seed_reset_workspace(vaws_repo)

    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_alpha").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_beta").returncode == 0
    assert run_vaws(vaws_repo, "session", "switch", "feat_beta").returncode == 0
    return simulation_root


def seed_initialized_reset_state_with_competing_target(vaws_repo: Path) -> Path:
    simulation_root = seed_reset_workspace(vaws_repo)
    add_competing_target(vaws_repo)

    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_alpha").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_beta").returncode == 0
    assert run_vaws(vaws_repo, "session", "switch", "feat_beta").returncode == 0
    return simulation_root


def test_reset_prepare_prints_destruction_summary_and_creates_confirmation_record(
    vaws_repo,
):
    seed_initialized_reset_state(vaws_repo)

    confirmation_id, output = prepare_reset_confirmation(vaws_repo)
    lowered = output.lower()
    assert confirmation_id in output
    assert "task 2" not in lowered
    assert "only clear local overlay state" not in lowered
    assert "wipe local workspace identity" in lowered
    assert "remote runtime context" in lowered
    assert (vaws_repo / ".workspace.local" / "reset-request.json").exists()


def test_reset_prepare_records_approved_target_in_confirmation_record(vaws_repo):
    seed_initialized_reset_state(vaws_repo)

    prepare_reset_confirmation(vaws_repo)
    request_data = read_json(vaws_repo / ".workspace.local" / "reset-request.json")

    assert request_data["approved_target"] == "single-default"


def test_reset_prepare_fails_closed_on_invalid_runtime_state(vaws_repo):
    seed_initialized_reset_state(vaws_repo)

    state_path = vaws_repo / ".workspace.local" / "state.json"
    state_path.write_text("not valid json", encoding="utf-8")

    result = run_vaws(vaws_repo, "reset", "--prepare")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid runtime state" in output
    assert "state.json" in output
    assert not (vaws_repo / ".workspace.local" / "reset-request.json").exists()


def test_reset_execute_fails_without_valid_confirmation_id(vaws_repo):
    seed_initialized_reset_state(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        "bogus-confirmation-id",
        "--confirm",
        "I authorize wiping local workspace identity and remote runtime",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "confirmation id" in output
    assert "invalid" in output or "unknown" in output
    assert confirmation_id not in output


def test_reset_execute_fails_without_confirmation_input(vaws_repo):
    seed_initialized_reset_state(vaws_repo)
    prepare_reset_confirmation(vaws_repo)

    result = run_vaws(vaws_repo, "reset", "--execute")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "confirmation" in output
    assert "missing" in output or "required" in output or "provide" in output


def test_reset_execute_fails_without_pending_reset_request(vaws_repo):
    seed_initialized_reset_state(vaws_repo)

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        "stale-confirmation-id",
        "--confirm",
        "I authorize wiping local workspace identity and remote runtime",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "reset-request" in output or "pending" in output or "prepare" in output


def test_reset_execute_fails_without_exact_confirmation_phrase(vaws_repo):
    seed_initialized_reset_state(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        confirmation_id,
        "--confirm",
        "this is not the exact phrase",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "confirmation phrase" in output
    assert "exact" in output or "match" in output


def test_reset_execute_fails_when_reset_request_status_is_not_pending(vaws_repo):
    seed_initialized_reset_state(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    request_path = vaws_repo / ".workspace.local" / "reset-request.json"
    request_data = read_json(request_path)
    request_data["status"] = "cancelled"
    request_path.write_text(json.dumps(request_data, indent=2) + "\n", encoding="utf-8")

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        confirmation_id,
        "--confirm",
        "I authorize wiping local workspace identity and remote runtime",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "status" in output
    assert "pending" in output


def test_reset_execute_rejects_confirmation_phrase_with_outer_whitespace(vaws_repo):
    seed_initialized_reset_state(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        confirmation_id,
        "--confirm",
        "  I authorize wiping local workspace identity and remote runtime  ",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "confirmation phrase" in output
    assert "exact" in output or "match" in output


def test_reset_execute_removes_local_and_remote_state(vaws_repo):
    seed_initialized_reset_state(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)
    simulation_root = vaws_repo.parent / "simulation-runtime"
    runtime_root = simulation_root / "host-a" / "vllm-workspace"

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        confirmation_id,
        "--confirm",
        "I authorize wiping local workspace identity and remote runtime",
    )

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "reset" in output
    assert "ok" in output or "complete" in output

    overlay = vaws_repo / ".workspace.local"
    assert not (overlay / "reset-request.json").exists()
    assert read_json(overlay / "state.json") == {}
    assert not (overlay / "sessions").exists()
    assert (overlay / "targets.yaml").read_text(encoding="utf-8") == ""
    assert (overlay / "auth.yaml").read_text(encoding="utf-8") == ""
    assert (overlay / "repos.yaml").read_text(encoding="utf-8") == ""
    assert not runtime_root.exists()
    assert not (runtime_root / ".vaws" / "current").exists()
    assert not (runtime_root / ".vaws" / "runtime.json").exists()
    assert _remote_url(vaws_repo, "vllm", "origin") == COMMUNITY_VLLM_URL
    assert _remote_url(vaws_repo, "vllm", "upstream") == COMMUNITY_VLLM_URL
    assert _remote_url(vaws_repo, "vllm-ascend", "origin") == COMMUNITY_VLLM_ASCEND_URL
    assert _remote_url(vaws_repo, "vllm-ascend", "upstream") == COMMUNITY_VLLM_ASCEND_URL


def test_reset_execute_keeps_cleanup_pinned_to_prepared_target_after_live_target_changes(
    vaws_repo,
):
    simulation_root = seed_initialized_reset_state_with_competing_target(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    assert run_vaws(vaws_repo, "target", "ensure", "single-backup").returncode == 0

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        confirmation_id,
        "--confirm",
        "I authorize wiping local workspace identity and remote runtime",
    )

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "ok" in output or "complete" in output

    prepared_runtime_root = simulation_root / "host-a" / "vllm-workspace"
    competing_runtime_root = simulation_root / "host-a" / "vllm-workspace-backup"
    assert not prepared_runtime_root.exists()
    assert competing_runtime_root.exists()
    assert not (vaws_repo / ".workspace.local" / "reset-request.json").exists()


def test_reset_execute_ignores_corrupted_live_state_when_prepared_target_is_pinned(
    vaws_repo,
):
    simulation_root = seed_initialized_reset_state(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    (vaws_repo / ".workspace.local" / "state.json").write_text(
        "not valid json",
        encoding="utf-8",
    )

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        confirmation_id,
        "--confirm",
        "I authorize wiping local workspace identity and remote runtime",
    )

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "ok" in output or "complete" in output

    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert not runtime_root.exists()
    assert not (vaws_repo / ".workspace.local" / "reset-request.json").exists()


def test_reset_execute_fails_and_preserves_request_when_sessions_cleanup_fails(
    vaws_repo,
):
    seed_initialized_reset_state(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    overlay = vaws_repo / ".workspace.local"
    sessions_path = overlay / "sessions"
    if sessions_path.exists():
        if sessions_path.is_dir():
            shutil.rmtree(sessions_path)
        else:
            sessions_path.unlink()
    sessions_path.write_text("not-a-directory", encoding="utf-8")

    result = run_vaws(
        vaws_repo,
        "reset",
        "--execute",
        "--confirmation-id",
        confirmation_id,
        "--confirm",
        "I authorize wiping local workspace identity and remote runtime",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "failed to clean local workspace" in output or "not a directory" in output
    assert "traceback (most recent call last)" not in output
    assert (overlay / "reset-request.json").exists()


def test_reset_execute_stops_before_local_cleanup_when_remote_cleanup_fails(
    vaws_repo,
):
    simulation_root = seed_reset_workspace(vaws_repo)
    overlay = vaws_repo / ".workspace.local"
    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0
    assert run_vaws(vaws_repo, "session", "create", "feat_alpha").returncode == 0
    assert run_vaws(vaws_repo, "session", "switch", "feat_alpha").returncode == 0

    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    runtime_parent = runtime_root.parent
    original_mode = runtime_parent.stat().st_mode
    runtime_parent.chmod(original_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)

    try:
        result = run_vaws(
            vaws_repo,
            "reset",
            "--execute",
            "--confirmation-id",
            confirmation_id,
            "--confirm",
            "I authorize wiping local workspace identity and remote runtime",
        )
    finally:
        runtime_parent.chmod(original_mode)

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "remote" in output or "cleanup" in output
    assert "permission" in output or "failed" in output

    assert read_json(overlay / "state.json")["current_session"] == "feat_alpha"
    assert (overlay / "sessions" / "feat_alpha" / "manifest.yaml").exists()
    assert (overlay / "reset-request.json").exists()
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert (runtime_root / ".vaws" / "current").exists()


def test_destroy_runtime_dispatches_host_cleanup(monkeypatch):
    calls = []

    def fake_run_host_command(ctx, script):
        calls.append(script)
        return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tools.lib.remote._run_host_command", fake_run_host_command)

    ctx = TargetContext(
        name="single-default",
        host=HostSpec(
            name="host-a",
            host="127.0.0.1",
            port=22,
            login_user="root",
            auth_group="shared-lab-a",
        ),
        credential=CredentialGroup(mode="ssh-key", username="root"),
        runtime=RuntimeSpec(
            image_ref="registry.example.com/ascend/vllm-ascend:test",
            container_name="vaws-owner",
            ssh_port=63269,
            workspace_root="/vllm-workspace",
            bootstrap_mode="host-then-container",
            host_workspace_path="/root/.vaws/targets/single-default/workspace",
            docker_run_args=[],
        ),
    )

    destroy_runtime(ctx)

    assert len(calls) == 1
    script = calls[0]
    assert "docker container ls -a" in script
    assert "|| exit 1" in script
    assert "docker rm -f" in script
    assert "grep -Fqx" in script or "grep -Fxq" in script
    assert "vaws-owner" in script
    assert "/root/.vaws/targets/single-default/workspace" in script
