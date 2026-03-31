import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import run_vaws, seed_overlay_files
from tools.lib import fleet
from tools.lib.config import RepoPaths


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
    assert "fleet add: ok" in output

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

    runtime_root = vaws_repo.parent / "simulation-runtime" / "host-b" / "vllm-workspace"
    runtime_state = json.loads((runtime_root / ".vaws" / "runtime.json").read_text())
    assert runtime_state["target"] == "host-b"
    assert runtime_state["container"]["created"] is True
    assert "reused" in runtime_state["container"]


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
        return {"verified": True}

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
