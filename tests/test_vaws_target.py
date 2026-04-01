"""Compatibility tests for the deprecated `vaws target` shim."""

import json
from pathlib import Path
import sys

import pytest
import yaml

from conftest import run_vaws

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.remote import RemoteError, resolve_server_context


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def seed_overlay(vaws_repo, target_name="single-default", use_modern_auth=False) -> Path:
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text("", encoding="utf-8")
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
    (overlay / "state.json").write_text("{}\n", encoding="utf-8")

    host_config = {
        "host": "10.0.0.11",
        "port": 22,
        "login_user": "root",
        "labels": ["a3", "8npu"],
    }
    if use_modern_auth:
        host_config["ssh_auth_ref"] = "shared-lab-a"
    else:
        host_config["auth_group"] = "shared-lab-a"

    targets = {
        "hosts": {
            "host-a": host_config,
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
    (overlay / "targets.yaml").write_text(yaml.safe_dump(targets), encoding="utf-8")
    return simulation_root


def test_target_ensure_reports_deprecation_and_routing(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "deprecated" in output
    assert "fleet" in output


def test_target_ensure_supports_modern_auth_compatibility(vaws_repo):
    simulation_root = seed_overlay(
        vaws_repo,
        target_name="single-default",
        use_modern_auth=True,
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 0
    state = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert state["schema_version"] == 1
    assert state["current_target"] == "single-default"
    assert state["current_target_kind"] == "legacy_target"
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert (runtime_root / ".vaws" / "runtime.json").exists()


def test_target_ensure_rejects_unsupported_state_schema_version(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    (vaws_repo / ".workspace.local" / "state.json").write_text(
        '{"schema_version": 2}\n',
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "schema_version" in output
    assert "unsupported" in output or "invalid" in output
    assert "traceback" not in output


def test_target_ensure_keeps_legacy_target_overlay_when_server_auth_exists(vaws_repo):
    simulation_root = seed_overlay(vaws_repo, target_name="single-default")
    overlay = vaws_repo / ".workspace.local"
    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "bootstrap": {
                    "completed": False,
                    "mode": "remote-first",
                },
                "servers": {
                    "host-a": {
                        "host": "10.0.0.11",
                        "port": 22,
                        "login_user": "root",
                        "ssh_auth_ref": "shared-lab-a",
                        "status": "ready",
                        "runtime": {
                            "image_ref": "registry.example.com/ascend/vllm-ascend:server",
                            "container_name": "fleet-owner",
                            "ssh_port": 63301,
                            "workspace_root": "/vllm-workspace",
                            "bootstrap_mode": "host-then-container",
                            "host_workspace_path": "/root/.vaws/targets/host-a/workspace",
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    targets = yaml.safe_load((overlay / "targets.yaml").read_text(encoding="utf-8"))
    targets["targets"]["single-default"]["runtime"]["ssh_port"] = 63269
    targets["targets"]["single-default"]["runtime"]["container_name"] = "legacy-owner"
    (overlay / "targets.yaml").write_text(
        yaml.safe_dump(targets, sort_keys=False),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "deprecated" in output
    state = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert state["current_target"] == "single-default"
    assert state["runtime"]["ssh_port"] == 63269
    assert state["runtime"]["container_name"] == "legacy-owner"
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert (runtime_root / ".vaws" / "runtime.json").exists()


def test_resolve_server_context_rejects_legacy_host_auth(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    simulation_root = seed_overlay(vaws_repo, target_name="single-default")
    (overlay / "servers.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "servers": {
                    "host-a": {
                        "host": "10.0.0.11",
                        "port": 22,
                        "login_user": "root",
                        "ssh_auth_ref": "shared-lab-a",
                        "status": "ready",
                        "runtime": {
                            "image_ref": "registry.example.com/ascend/vllm-ascend:test",
                            "container_name": "fleet-owner",
                            "ssh_port": 63301,
                            "workspace_root": "/vllm-workspace",
                            "bootstrap_mode": "host-then-container",
                            "host_workspace_path": "/root/.vaws/targets/host-a/workspace",
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
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
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RemoteError, match="missing ssh_auth\\.refs map"):
        resolve_server_context(RepoPaths(root=vaws_repo), "host-a")
