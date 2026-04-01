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
from tools.lib import reset
from tools.lib.config import RepoPaths
from tools.lib.lifecycle_state import LEGACY_TARGET_HANDOFF_KIND
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


def seed_reset_workspace(
    vaws_repo: Path,
    target_name: str = "single-default",
    use_modern_auth: bool = False,
) -> Path:
    simulation_root = vaws_repo.parent / "simulation-runtime"
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)

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
            },
            "git_auth": {"refs": {}},
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
            },
            "git_auth": {"refs": {}},
        }
    )
    (overlay / "auth.yaml").write_text(yaml.safe_dump(auth_config), encoding="utf-8")
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


def seed_reset_workspace_with_servers(vaws_repo: Path) -> Path:
    simulation_root = seed_reset_workspace(vaws_repo)
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
                    "host-a": {
                        "host": "127.0.0.1",
                        "port": 22,
                        "login_user": "root",
                        "ssh_auth_ref": "shared-lab-a",
                        "status": "ready",
                        "runtime": {
                            "image_ref": "registry.example.com/ascend/vllm-ascend:test",
                            "container_name": "vaws-owner",
                            "ssh_port": 63269,
                            "workspace_root": "/vllm-workspace",
                            "bootstrap_mode": "host-then-container",
                            "host_workspace_path": "/root/.vaws/targets/single-default/workspace",
                        },
                    },
                    "host-b": {
                        "host": "127.0.0.2",
                        "port": 22,
                        "login_user": "root",
                        "ssh_auth_ref": "shared-lab-a",
                        "status": "ready",
                        "runtime": {
                            "image_ref": "registry.example.com/ascend/vllm-ascend:test",
                            "container_name": "vaws-backup",
                            "ssh_port": 63270,
                            "workspace_root": "/vllm-workspace-backup",
                            "bootstrap_mode": "host-then-container",
                            "host_workspace_path": "/root/.vaws/targets/single-backup/workspace",
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
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


def _remove_current_target_kind(vaws_repo: Path) -> None:
    overlay = vaws_repo / ".workspace.local"
    state = read_json(overlay / "state.json")
    state.pop("current_target_kind", None)
    (overlay / "state.json").write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_reset_confirmation(vaws_repo: Path):
    overlay = vaws_repo / ".workspace.local"
    result = run_vaws(vaws_repo, "reset", "--prepare")
    assert result.returncode == 0

    output = result.stdout + result.stderr
    lowered = output.lower()
    assert "wipe local workspace identity" in lowered
    assert "managed servers" in lowered
    assert "approved target" in lowered or "approved-target" in lowered
    assert "managed-server cleanup" in lowered or "approved-target cleanup" in lowered
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
    assert "managed servers" in lowered
    assert "approved target" in lowered or "approved-target" in lowered
    assert "managed-server cleanup" in lowered or "approved-target cleanup" in lowered
    assert "active bootstrap target" not in lowered
    assert (vaws_repo / ".workspace.local" / "reset-request.json").exists()


def test_reset_prepare_records_approved_target_in_confirmation_record(vaws_repo):
    seed_initialized_reset_state(vaws_repo)

    prepare_reset_confirmation(vaws_repo)
    request_data = read_json(vaws_repo / ".workspace.local" / "reset-request.json")

    assert request_data["approved_target"] == "single-default"
    assert request_data["approved_target_kind"] == LEGACY_TARGET_HANDOFF_KIND


def test_reset_execute_refuses_malformed_servers_inventory_and_leaves_local_state_intact(
    vaws_repo,
):
    seed_initialized_reset_state(vaws_repo)
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    overlay = vaws_repo / ".workspace.local"
    (overlay / "servers.yaml").write_text("not: [valid", encoding="utf-8")
    state_before = read_json(overlay / "state.json")

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
    assert "servers.yaml" in output
    assert "invalid" in output
    assert (overlay / "reset-request.json").exists()
    assert read_json(overlay / "state.json") == state_before


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


def test_reset_execute_cleans_remote_before_local(vaws_repo, monkeypatch):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    (overlay / "reset-request.json").write_text(
        json.dumps(
            {
                "confirmation_id": "reset-1",
                "status": "pending",
                "confirmation_phrase": reset.CONFIRMATION_PHRASE,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    calls = []
    monkeypatch.setattr(
        reset,
        "_cleanup_remote_state",
        lambda request, paths: calls.append("remote") or [],
    )
    monkeypatch.setattr(
        reset,
        "_cleanup_local_state",
        lambda paths: calls.append("local"),
    )
    monkeypatch.setattr(reset, "_restore_public_repo_remotes", lambda paths: None)

    result = reset.execute_reset(
        RepoPaths(root=vaws_repo),
        "reset-1",
        reset.CONFIRMATION_PHRASE,
    )

    assert result == 0
    assert calls == ["remote", "local"]


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
    assert read_json(overlay / "state.json") == {"schema_version": 1}
    assert not (overlay / "sessions").exists()
    assert (overlay / "targets.yaml").read_text(encoding="utf-8") == ""
    assert yaml.safe_load((overlay / "servers.yaml").read_text(encoding="utf-8")) == {
        "version": 1,
        "servers": {},
    }
    assert (overlay / "auth.yaml").read_text(encoding="utf-8") == ""
    assert (overlay / "repos.yaml").read_text(encoding="utf-8") == ""
    assert not runtime_root.exists()
    assert not (runtime_root / ".vaws" / "current").exists()
    assert not (runtime_root / ".vaws" / "runtime.json").exists()
    assert _remote_url(vaws_repo, "vllm", "origin") == COMMUNITY_VLLM_URL
    assert _remote_url(vaws_repo, "vllm", "upstream") == COMMUNITY_VLLM_URL
    assert _remote_url(vaws_repo, "vllm-ascend", "origin") == COMMUNITY_VLLM_ASCEND_URL
    assert _remote_url(vaws_repo, "vllm-ascend", "upstream") == COMMUNITY_VLLM_ASCEND_URL


def test_reset_execute_removes_remote_runtime_for_legacy_host_auth_managed_server(
    vaws_repo,
):
    simulation_root = seed_reset_workspace(vaws_repo)
    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0

    overlay = vaws_repo / ".workspace.local"
    auth = yaml.safe_load((overlay / "auth.yaml").read_text(encoding="utf-8"))
    auth.pop("ssh_auth", None)
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(auth, sort_keys=False),
        encoding="utf-8",
    )

    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "bootstrap": {
                    "completed": True,
                    "mode": "remote-first",
                },
                "servers": {
                    "host-a": {
                        "host": "127.0.0.1",
                        "port": 22,
                        "login_user": "root",
                        "auth_group": "shared-lab-a",
                        "status": "ready",
                        "runtime": {
                            "image_ref": "registry.example.com/ascend/vllm-ascend:test",
                            "container_name": "vaws-owner",
                            "ssh_port": 63269,
                            "workspace_root": "/vllm-workspace",
                            "bootstrap_mode": "host-then-container",
                            "host_workspace_path": "/root/.vaws/targets/single-default/workspace",
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

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
    assert "host-a" in output
    assert "removed" in output
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert not runtime_root.exists()


def test_reset_execute_surfaces_managed_server_auth_failure_without_fallback(
    vaws_repo,
):
    simulation_root = seed_reset_workspace_with_servers(vaws_repo)
    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0

    overlay = vaws_repo / ".workspace.local"
    auth = yaml.safe_load((overlay / "auth.yaml").read_text(encoding="utf-8"))
    auth.pop("ssh_auth", None)
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(auth, sort_keys=False),
        encoding="utf-8",
    )

    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

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
    assert "host-a" in output
    assert "host-b" in output
    assert "cleanup_failed" in output
    assert "missing ssh_auth.refs map" in output
    assert "unknown target" not in output
    assert "single-default" in output
    assert "removed" in output
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert not runtime_root.exists()


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


def test_reset_execute_attempts_cleanup_for_all_managed_servers(
    vaws_repo,
    monkeypatch,
    capsys,
):
    seed_reset_workspace_with_servers(vaws_repo)
    overlay = vaws_repo / ".workspace.local"
    confirmation_id = "reset-managed"
    (overlay / "reset-request.json").write_text(
        json.dumps(
            {
                "confirmation_id": confirmation_id,
                "status": "pending",
                "confirmation_phrase": reset.CONFIRMATION_PHRASE,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    calls = []

    class FakeCleanupResult:
        def __init__(self, server_name: str):
            self.server_name = server_name
            self.status = "removed"
            self.detail = "removed"

        def to_mapping(self):
            return {
                "server_name": self.server_name,
                "status": self.status,
                "detail": self.detail,
            }

    def fake_cleanup_server_runtime(paths, server_name):
        calls.append(server_name)
        return FakeCleanupResult(server_name)

    monkeypatch.setattr(
        reset,
        "_cleanup_managed_server_runtime",
        fake_cleanup_server_runtime,
        raising=False,
    )

    result = reset.execute_reset(
        RepoPaths(root=vaws_repo),
        confirmation_id,
        "I authorize wiping local workspace identity and remote runtime",
    )
    output = capsys.readouterr().out.lower()

    assert result == 0
    assert calls == ["host-a", "host-b"]
    assert "host-a" in output
    assert "host-b" in output
    assert "removed" in output


def test_reset_execute_attempts_cleanup_for_managed_servers_and_approved_target(
    vaws_repo,
    monkeypatch,
    capsys,
):
    seed_reset_workspace_with_servers(vaws_repo)
    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    calls = []

    class FakeCleanupResult:
        def __init__(self, server_name: str):
            self.server_name = server_name
            self.status = "removed"
            self.detail = "removed"

        def to_mapping(self):
            return {
                "server_name": self.server_name,
                "status": self.status,
                "detail": self.detail,
            }

    def fake_cleanup_server_runtime(paths, server_name):
        calls.append(server_name)
        return FakeCleanupResult(server_name)

    monkeypatch.setattr(
        reset,
        "_cleanup_managed_server_runtime",
        fake_cleanup_server_runtime,
        raising=False,
    )
    monkeypatch.setattr(
        reset,
        "cleanup_target_runtime",
        fake_cleanup_server_runtime,
        raising=False,
    )

    result = reset.execute_reset(
        RepoPaths(root=vaws_repo),
        confirmation_id,
        "I authorize wiping local workspace identity and remote runtime",
    )
    output = capsys.readouterr().out.lower()

    assert result == 0
    assert calls == ["host-a", "host-b", "single-default"]
    assert "host-a" in output
    assert "host-b" in output
    assert "single-default" in output
    assert "removed" in output


def test_reset_execute_cleans_prepared_managed_target_even_if_removed_from_inventory(
    vaws_repo,
    monkeypatch,
    capsys,
):
    seed_reset_workspace_with_servers(vaws_repo)
    overlay = vaws_repo / ".workspace.local"
    (overlay / "reset-request.json").write_text(
        json.dumps(
            {
                "confirmation_id": "reset-managed-approved",
                "status": "pending",
                "confirmation_phrase": reset.CONFIRMATION_PHRASE,
                "approved_target": "host-c",
                "approved_target_kind": "managed_server",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    calls = []

    class FakeCleanupResult:
        def __init__(self, server_name: str):
            self.server_name = server_name
            self.status = "removed"
            self.detail = "removed"

        def to_mapping(self):
            return {
                "server_name": self.server_name,
                "status": self.status,
                "detail": self.detail,
            }

    def fake_cleanup_managed_server_runtime(paths, server_name):
        calls.append(server_name)
        return FakeCleanupResult(server_name)

    monkeypatch.setattr(
        reset,
        "_cleanup_managed_server_runtime",
        fake_cleanup_managed_server_runtime,
        raising=False,
    )

    result = reset.execute_reset(
        RepoPaths(root=vaws_repo),
        "reset-managed-approved",
        "I authorize wiping local workspace identity and remote runtime",
    )
    output = capsys.readouterr().out.lower()

    assert result == 0
    assert calls == ["host-a", "host-b", "host-c"]
    assert "host-c" in output
    assert "removed" in output


def test_reset_execute_cleans_managed_server_and_legacy_target_collision_separately(
    vaws_repo,
    monkeypatch,
    capsys,
):
    simulation_root = seed_reset_workspace(vaws_repo, target_name="shared-name")
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
                    "shared-name": {
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
                            "host_workspace_path": "/root/.vaws/targets/shared-name/workspace",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert run_vaws(vaws_repo, "target", "ensure", "shared-name").returncode == 0
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)

    calls = []

    class FakeCleanupResult:
        def __init__(self, server_name: str, status: str):
            self.server_name = server_name
            self.status = status
            self.detail = status

        def to_mapping(self):
            return {
                "server_name": self.server_name,
                "status": self.status,
                "detail": self.detail,
            }

    def fake_cleanup_managed_server_runtime(paths, server_name):
        calls.append(("managed", server_name))
        return FakeCleanupResult(server_name, "removed")

    def fake_cleanup_target_runtime(paths, target_name):
        calls.append(("legacy", target_name))
        return FakeCleanupResult(target_name, "removed")

    monkeypatch.setattr(reset, "_cleanup_managed_server_runtime", fake_cleanup_managed_server_runtime, raising=False)
    monkeypatch.setattr(reset, "cleanup_target_runtime", fake_cleanup_target_runtime, raising=False)

    result = reset.execute_reset(
        RepoPaths(root=vaws_repo),
        confirmation_id,
        "I authorize wiping local workspace identity and remote runtime",
    )
    output = capsys.readouterr().out.lower()

    assert result == 0
    assert calls == [("managed", "shared-name"), ("legacy", "shared-name")]
    assert "shared-name" in output
    assert "removed" in output


def test_reset_execute_infers_missing_current_target_kind_for_modern_auth_collision(
    vaws_repo,
):
    simulation_root = seed_reset_workspace(
        vaws_repo,
        target_name="shared-name",
        use_modern_auth=True,
    )
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
                    "shared-name": {
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
                            "host_workspace_path": "/root/.vaws/targets/shared-name/workspace",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    assert run_vaws(vaws_repo, "target", "ensure", "shared-name").returncode == 0
    _remove_current_target_kind(vaws_repo)

    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)
    request_path = overlay / "reset-request.json"
    request_data = read_json(request_path)
    assert request_data["approved_target"] == "shared-name"
    assert request_data["approved_target_kind"] == LEGACY_TARGET_HANDOFF_KIND
    request_data.pop("approved_target_kind")
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

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "shared-name" in output
    assert "ok" in output or "complete" in output
    target_runtime_root = simulation_root / "host-a" / "vllm-workspace"
    server_runtime_root = simulation_root / "shared-name" / "vllm-workspace"
    assert not target_runtime_root.exists()
    assert not server_runtime_root.exists()
    assert read_json(overlay / "state.json") == {"schema_version": 1}


def test_reset_execute_reports_unreachable_servers_but_continues_cleanup(
    vaws_repo,
    monkeypatch,
    capsys,
):
    seed_reset_workspace_with_servers(vaws_repo)
    assert run_vaws(vaws_repo, "target", "ensure", "single-default").returncode == 0
    confirmation_id, _ = prepare_reset_confirmation(vaws_repo)
    overlay = vaws_repo / ".workspace.local"

    class FakeCleanupResult:
        def __init__(self, server_name: str, status: str, detail: str):
            self.server_name = server_name
            self.status = status
            self.detail = detail

        def to_mapping(self):
            return {
                "server_name": self.server_name,
                "status": self.status,
                "detail": self.detail,
            }

    def fake_cleanup_managed_server_runtime(paths, server_name):
        if server_name == "host-a":
            return FakeCleanupResult(server_name, "removed", "removed")
        return FakeCleanupResult(server_name, "unreachable", "ssh probe failed")

    monkeypatch.setattr(
        reset,
        "_cleanup_managed_server_runtime",
        fake_cleanup_managed_server_runtime,
        raising=False,
    )

    result = reset.execute_reset(
        RepoPaths(root=vaws_repo),
        confirmation_id,
        "I authorize wiping local workspace identity and remote runtime",
    )
    output = capsys.readouterr().out.lower()

    assert result == 0
    assert "host-b" in output
    assert "unreachable" in output
    assert not (overlay / "reset-request.json").exists()
    assert read_json(overlay / "state.json") == {"schema_version": 1}


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
