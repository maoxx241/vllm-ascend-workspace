from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.overlay import ensure_overlay_layout
from tools.lib.target_context import resolve_server_context


def test_remote_module_is_deleted():
    assert not (ROOT / "tools/lib/remote.py").exists()


def test_peer_mesh_test_is_deleted():
    assert not (ROOT / "tests/test_peer_mesh.py").exists()


def test_capability_state_module_and_legacy_registry_tests_are_deleted():
    assert not (ROOT / "tools/lib/capability_state.py").exists()
    assert not (ROOT / "tests/test_capability_state.py").exists()
    assert not (ROOT / "tests/test_serving_registry.py").exists()


def test_overlay_init_does_not_create_state_or_targets(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)

    assert not (vaws_repo / ".workspace.local/state.json").exists()
    assert not (vaws_repo / ".workspace.local/targets.yaml").exists()


def test_target_context_no_longer_accepts_production_local_simulation(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    paths.local_auth_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "ssh_auth": {
                    "refs": {
                        "sim-auth": {
                            "kind": "local-simulation",
                            "username": "root",
                            "simulation_root": "/tmp/vaws-sim",
                        }
                    }
                },
                "git_auth": {"refs": {}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    paths.local_servers_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "servers": {
                    "sim-box": {
                        "host": "sim.local",
                        "port": 22,
                        "login_user": "root",
                        "ssh_auth_ref": "sim-auth",
                        "runtime": {
                            "image_ref": "image",
                            "container_name": "container",
                            "ssh_port": 41001,
                            "bootstrap_mode": "host-then-container",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="local-simulation"):
        resolve_server_context(paths, "sim-box")
