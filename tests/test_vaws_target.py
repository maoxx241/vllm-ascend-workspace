import json

import yaml

from conftest import run_vaws


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def seed_overlay(vaws_repo, target_name="single-default"):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "repos.yaml").write_text("", encoding="utf-8")
    (overlay / "auth.yaml").write_text("", encoding="utf-8")
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


def test_target_ensure_records_runtime_endpoint(vaws_repo):
    seed_overlay(vaws_repo, target_name="single-default")
    result = run_vaws(vaws_repo, "target", "ensure", "single-default")
    assert result.returncode == 0
    state = read_json(vaws_repo / ".workspace.local" / "state.json")
    assert state["current_target"] == "single-default"
    assert state["runtime"]["workspace_root"] == "/vllm-workspace"
    assert state["runtime"]["ssh_port"] == 63269


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
