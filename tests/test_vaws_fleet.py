import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import run_vaws, seed_overlay_files
from tools.lib import fleet
from tools.lib.config import RepoPaths
from tools.lib.remote import (
    VerificationCheck,
    VerificationResult,
    ensure_runtime,
    resolve_server_context,
    verify_runtime,
)


def _write_fleet_overlay(vaws_repo: Path) -> Path:
    seed_overlay_files(vaws_repo)
    overlay = vaws_repo / ".workspace.local"
    simulation_root = vaws_repo.parent / "simulation-runtime"
    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "bootstrap": {
                    "completed": True,
                    "mode": "remote-first",
                },
                "servers": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "ssh_auth": {
                    "refs": {
                        "default-server-auth": {
                            "kind": "local-simulation",
                            "username": "root",
                            "simulation_root": str(simulation_root),
                        }
                    }
                },
                "git_auth": {
                    "refs": {
                        "default-git-auth": {
                            "kind": "ssh-agent",
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return overlay


def _write_ssh_auth_refs(vaws_repo: Path, *ref_names: str) -> None:
    overlay = vaws_repo / ".workspace.local"
    auth = yaml.safe_load((overlay / "auth.yaml").read_text(encoding="utf-8"))
    simulation_root = vaws_repo.parent / "simulation-runtime"
    auth["ssh_auth"]["refs"] = {
        ref_name: {
            "kind": "local-simulation",
            "username": "root",
            "simulation_root": str(simulation_root),
        }
        for ref_name in ref_names
    }
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(auth, sort_keys=False),
        encoding="utf-8",
    )


def _write_legacy_host_auth(vaws_repo: Path, *group_names: str) -> None:
    overlay = vaws_repo / ".workspace.local"
    simulation_root = vaws_repo.parent / "simulation-runtime"
    (overlay / "auth.yaml").write_text(
        yaml.safe_dump(
            {
                "host_auth": {
                    "mode": "local-simulation",
                    "credential_groups": {
                        group_name: {
                            "username": "root",
                            "simulation_root": str(simulation_root),
                        }
                        for group_name in group_names
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_fleet_add_writes_server_inventory_entry(vaws_repo):
    _write_fleet_overlay(vaws_repo)

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
        "--ssh-auth-ref",
        "default-server-auth",
    )

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "fleet add: ready" in output

    servers = yaml.safe_load((vaws_repo / ".workspace.local" / "servers.yaml").read_text())
    host_b = servers["servers"]["host-b"]
    assert host_b["host"] == "10.0.0.12"
    assert host_b["port"] == 22
    assert host_b["login_user"] == "root"
    assert host_b["ssh_auth_ref"] == "default-server-auth"
    assert host_b["status"] == "ready"
    assert host_b["runtime"]["image_ref"]
    assert host_b["runtime"]["container_name"]
    assert host_b["runtime"]["ssh_port"] == 63269
    assert host_b["runtime"]["workspace_root"] == "/vllm-workspace"
    assert host_b["runtime"]["host_workspace_path"].endswith("/host-b/workspace")
    assert host_b["verification"]["status"] == "ready"
    assert host_b["verification"]["checks"][0]["name"] == "runtime_state"
    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["server_verifications"]["host-b"]["status"] == "ready"

    runtime_root = vaws_repo.parent / "simulation-runtime" / "host-b" / "vllm-workspace"
    runtime_state = json.loads((runtime_root / ".vaws" / "runtime.json").read_text())
    assert runtime_state["target"] == "host-b"
    assert runtime_state["container"]["created"] is True
    assert "reused" in runtime_state["container"]


def test_fleet_add_infers_single_available_ssh_auth_ref(vaws_repo):
    _write_fleet_overlay(vaws_repo)
    _write_ssh_auth_refs(vaws_repo, "lab-sim")

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
    )

    assert result.returncode == 0
    servers = yaml.safe_load((vaws_repo / ".workspace.local" / "servers.yaml").read_text())
    assert servers["servers"]["host-b"]["ssh_auth_ref"] == "lab-sim"


def test_fleet_add_requires_bootstrap_before_auth_resolution(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert "init --bootstrap" in output
    assert "auth config" not in output


def test_fleet_add_requires_completed_bootstrap_baseline(vaws_repo):
    _write_fleet_overlay(vaws_repo)
    overlay = vaws_repo / ".workspace.local"
    servers = yaml.safe_load((overlay / "servers.yaml").read_text(encoding="utf-8"))
    servers["bootstrap"]["completed"] = False
    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(servers, sort_keys=False),
        encoding="utf-8",
    )

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert "init --bootstrap" in output


def test_fleet_add_requires_bootstrap_map_in_server_inventory(vaws_repo):
    _write_fleet_overlay(vaws_repo)
    overlay = vaws_repo / ".workspace.local"
    servers = yaml.safe_load((overlay / "servers.yaml").read_text(encoding="utf-8"))
    servers.pop("bootstrap", None)
    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(servers, sort_keys=False),
        encoding="utf-8",
    )

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert "init --bootstrap" in output


def test_fleet_add_infers_single_legacy_host_auth_group(vaws_repo):
    _write_fleet_overlay(vaws_repo)
    _write_legacy_host_auth(vaws_repo, "shared-lab-a")

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
    )

    assert result.returncode == 0
    servers = yaml.safe_load((vaws_repo / ".workspace.local" / "servers.yaml").read_text())
    assert servers["servers"]["host-b"]["ssh_auth_ref"] == "shared-lab-a"


def test_fleet_add_requires_explicit_ssh_auth_ref_when_multiple_legacy_groups_exist(vaws_repo):
    _write_fleet_overlay(vaws_repo)
    _write_legacy_host_auth(vaws_repo, "shared-lab-a", "shared-lab-b")

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "multiple" in output
    assert "ssh auth ref" in output
    assert "shared-lab-a" in output
    assert "shared-lab-b" in output


def test_fleet_add_requires_explicit_ssh_auth_ref_when_multiple_refs_exist(vaws_repo):
    _write_fleet_overlay(vaws_repo)
    _write_ssh_auth_refs(vaws_repo, "lab-a", "lab-b")

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "multiple" in output
    assert "ssh auth ref" in output
    assert "lab-a" in output
    assert "lab-b" in output


def test_remote_verify_runtime_returns_explicit_result_shape(vaws_repo):
    overlay = _write_fleet_overlay(vaws_repo)
    servers = yaml.safe_load((overlay / "servers.yaml").read_text())
    servers["servers"]["host-a"] = {
        "host": "10.0.0.11",
        "port": 22,
        "login_user": "root",
        "ssh_auth_ref": "default-server-auth",
        "status": "pending",
        "runtime": {
            "image_ref": "quay.nju.edu.cn/ascend/vllm-ascend:latest",
            "container_name": "vaws-workspace",
            "ssh_port": 63269,
            "workspace_root": "/vllm-workspace",
            "bootstrap_mode": "host-then-container",
        },
    }
    overlay.joinpath("servers.yaml").write_text(
        yaml.safe_dump(servers, sort_keys=False),
        encoding="utf-8",
    )
    paths = RepoPaths(root=vaws_repo)
    context = resolve_server_context(paths, "host-a")
    ensure_runtime(paths, context)

    result = verify_runtime(paths, context)

    assert isinstance(result, VerificationResult)
    assert result.status == "ready"
    assert result.to_mapping()["status"] == "ready"
    assert result.to_mapping()["checks"][0]["name"] == "runtime_state"
    assert result.to_mapping()["runtime"]["host_name"] == "host-a"


def test_fleet_list_reports_inventory_entries(vaws_repo):
    overlay = _write_fleet_overlay(vaws_repo)
    servers = yaml.safe_load((overlay / "servers.yaml").read_text())
    servers["servers"]["host-a"] = {
        "host": "10.0.0.11",
        "port": 22,
        "login_user": "root",
        "ssh_auth_ref": "default-server-auth",
        "status": "pending",
        "runtime": {
            "image_ref": "quay.nju.edu.cn/ascend/vllm-ascend:latest",
            "container_name": "vaws-workspace",
            "ssh_port": 63269,
            "workspace_root": "/vllm-workspace",
            "bootstrap_mode": "host-then-container",
        },
    }
    overlay.joinpath("servers.yaml").write_text(
        yaml.safe_dump(servers, sort_keys=False),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "fleet", "list")

    assert result.returncode == 0
    output = result.stdout.lower()
    assert "host-a" in output
    assert "pending" in output


def test_fleet_add_rejects_invalid_server_port(vaws_repo):
    _write_fleet_overlay(vaws_repo)

    result = run_vaws(
        vaws_repo,
        "fleet",
        "add",
        "host-b",
        "--server-host",
        "10.0.0.12",
        "--ssh-auth-ref",
        "default-server-auth",
        "--server-port",
        "70000",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "port" in output
    assert "traceback" not in output


def test_fleet_add_records_needs_repair_when_verification_needs_repair(vaws_repo, monkeypatch):
    _write_fleet_overlay(vaws_repo)

    class FakeVerificationResult:
        status = "needs_repair"
        summary = "runtime probe failed"

        def to_mapping(self):
            return {
                "status": self.status,
                "summary": self.summary,
                "checks": [
                    {
                        "name": "runtime_state",
                        "status": "needs_repair",
                        "detail": "probe failed",
                    }
                ],
            }

    monkeypatch.setattr(fleet, "verify_runtime", lambda *args, **kwargs: FakeVerificationResult())

    result = fleet.add_fleet_server(
        RepoPaths(root=vaws_repo),
        "host-b",
        "10.0.0.12",
        "default-server-auth",
    )
    assert result == 0
    servers = yaml.safe_load((vaws_repo / ".workspace.local" / "servers.yaml").read_text())
    host_b = servers["servers"]["host-b"]
    assert host_b["status"] == "needs_repair"
    assert host_b["verification"]["status"] == "needs_repair"
    assert host_b["verification"]["checks"][0]["detail"] == "probe failed"

    state = json.loads((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["server_verifications"]["host-b"]["status"] == "needs_repair"


def test_fleet_verify_is_read_only(vaws_repo, monkeypatch):
    overlay = _write_fleet_overlay(vaws_repo)
    servers = yaml.safe_load((overlay / "servers.yaml").read_text())
    servers["servers"]["host-a"] = {
        "host": "10.0.0.11",
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
        },
    }
    overlay.joinpath("servers.yaml").write_text(
        yaml.safe_dump(servers, sort_keys=False),
        encoding="utf-8",
    )
    state_path = overlay / "state.json"
    state_before = state_path.read_text(encoding="utf-8")
    servers_before = (overlay / "servers.yaml").read_text(encoding="utf-8")

    calls = {}

    def fake_verify_runtime(paths, ctx):
        calls["server"] = ctx.name
        return VerificationResult.ready(
            summary="runtime verified",
            runtime={
                "host_name": ctx.name,
                "host": ctx.host.host,
                "host_port": ctx.host.port,
                "login_user": ctx.host.login_user,
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

    result = fleet.verify_fleet_server(RepoPaths(root=vaws_repo), "host-a")

    assert result == 0
    assert calls["server"] == "host-a"
    assert state_path.read_text(encoding="utf-8") == state_before
    assert (overlay / "servers.yaml").read_text(encoding="utf-8") == servers_before


def test_fleet_verify_requires_known_server(vaws_repo):
    _write_fleet_overlay(vaws_repo)

    result = run_vaws(vaws_repo, "fleet", "verify", "missing")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "missing" in output
    assert "server" in output
