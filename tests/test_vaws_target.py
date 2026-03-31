import json
from pathlib import Path

import yaml

from conftest import run_vaws


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def seed_overlay(vaws_repo, target_name="single-default") -> Path:
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text("", encoding="utf-8")
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
    (overlay / "state.json").write_text("{}\n", encoding="utf-8")

    targets = {
        "hosts": {
            "host-a": {
                "host": "10.0.0.11",
                "port": 22,
                "login_user": "root",
                "auth_group": "shared-lab-a",
                "labels": ["a3", "8npu"],
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
    (overlay / "targets.yaml").write_text(yaml.safe_dump(targets), encoding="utf-8")
    return simulation_root


def test_target_ensure_records_runtime_endpoint(vaws_repo):
    simulation_root = seed_overlay(vaws_repo, target_name="single-default")
    result = run_vaws(vaws_repo, "target", "ensure", "single-default")
    assert result.returncode == 0
    state = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert state["schema_version"] == 1
    assert state["current_target"] == "single-default"
    assert state["runtime"]["workspace_root"] == "/vllm-workspace"
    assert state["runtime"]["ssh_port"] == 63269
    assert state["runtime"]["container_endpoint"].startswith("simulation://")
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    assert (runtime_root / ".vaws" / "runtime.json").exists()
    assert (runtime_root / "workspace").is_symlink()


def test_target_ensure_reuses_existing_runtime_container(vaws_repo):
    simulation_root = seed_overlay(vaws_repo, target_name="single-default")

    first = run_vaws(vaws_repo, "target", "ensure", "single-default")
    second = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert first.returncode == 0
    assert second.returncode == 0
    state = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert state["schema_version"] == 1
    runtime_root = simulation_root / "host-a" / "vllm-workspace"
    runtime_state = read_json(runtime_root / ".vaws" / "runtime.json")
    assert runtime_state["container"]["created"] is True
    assert runtime_state["container"]["reused"] is True


def test_target_ensure_fails_cleanly_when_state_schema_version_is_unsupported(vaws_repo):
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


def test_target_ensure_fails_when_target_is_missing(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    result = run_vaws(vaws_repo, "target", "ensure", "unknown")
    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "unknown" in output
    assert "target" in output


def test_target_ensure_fails_when_targets_yaml_is_invalid_utf8(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    (vaws_repo / ".workspace.local" / "targets.yaml").write_bytes(b"\xff\xfe")

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid target config" in output
    assert "traceback" not in output


def test_target_ensure_fails_when_target_runtime_mapping_is_missing(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")

    targets_without_runtime = {
        "hosts": {
            "host-a": {
                "host": "10.0.0.11",
                "port": 22,
                "login_user": "root",
                "auth_group": "shared-lab-a",
                "labels": ["a3", "8npu"],
            }
        },
        "targets": {
            "single-default": {
                "hosts": ["host-a"],
            }
        },
    }
    (vaws_repo / ".workspace.local" / "targets.yaml").write_text(
        yaml.safe_dump(targets_without_runtime), encoding="utf-8"
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "runtime" in output


def test_target_ensure_fails_when_target_runtime_mapping_is_incomplete(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")

    targets_with_incomplete_runtime = {
        "hosts": {
            "host-a": {
                "host": "10.0.0.11",
                "port": 22,
                "login_user": "root",
                "auth_group": "shared-lab-a",
                "labels": ["a3", "8npu"],
            }
        },
        "targets": {
            "single-default": {
                "hosts": ["host-a"],
                "runtime": {
                    "workspace_root": "/vllm-workspace",
                },
            }
        },
    }
    (vaws_repo / ".workspace.local" / "targets.yaml").write_text(
        yaml.safe_dump(targets_with_incomplete_runtime), encoding="utf-8"
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "runtime" in output
    assert "incomplete" in output


def test_target_ensure_fails_when_target_runtime_ssh_port_is_bool(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")

    targets = yaml.safe_load(
        (vaws_repo / ".workspace.local" / "targets.yaml").read_text(encoding="utf-8")
    )
    targets["targets"]["single-default"]["runtime"]["ssh_port"] = True
    (vaws_repo / ".workspace.local" / "targets.yaml").write_text(
        yaml.safe_dump(targets), encoding="utf-8"
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "ssh_port" in output


def test_target_ensure_fails_when_target_runtime_ssh_port_is_out_of_range(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")

    targets = yaml.safe_load(
        (vaws_repo / ".workspace.local" / "targets.yaml").read_text(encoding="utf-8")
    )
    targets["targets"]["single-default"]["runtime"]["ssh_port"] = 70000
    (vaws_repo / ".workspace.local" / "targets.yaml").write_text(
        yaml.safe_dump(targets), encoding="utf-8"
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "ssh_port" in output


def test_target_ensure_defaults_blank_workspace_root(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")

    targets = yaml.safe_load(
        (vaws_repo / ".workspace.local" / "targets.yaml").read_text(encoding="utf-8")
    )
    targets["targets"]["single-default"]["runtime"]["workspace_root"] = ""
    (vaws_repo / ".workspace.local" / "targets.yaml").write_text(
        yaml.safe_dump(targets), encoding="utf-8"
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 0
    state = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert state["runtime"]["workspace_root"] == "/vllm-workspace"


def test_target_ensure_fails_when_auth_group_is_missing(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")

    targets = yaml.safe_load(
        (vaws_repo / ".workspace.local" / "targets.yaml").read_text(encoding="utf-8")
    )
    targets["hosts"]["host-a"]["auth_group"] = "missing-group"
    (vaws_repo / ".workspace.local" / "targets.yaml").write_text(
        yaml.safe_dump(targets), encoding="utf-8"
    )

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "auth" in output
    assert "missing-group" in output


def test_target_ensure_reports_deprecation_and_routing(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")

    result = run_vaws(vaws_repo, "target", "ensure", "single-default")

    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert "deprecated" in output
    assert "fleet" in output
