import importlib
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.overlay import ensure_overlay_layout


def _write_auth(paths: RepoPaths, payload: dict) -> None:
    paths.local_auth_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_servers(paths: RepoPaths, payload: dict) -> None:
    paths.local_servers_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_target_context_module_exists_and_is_importable():
    module_path = ROOT / "tools/lib/target_context.py"
    assert module_path.exists()
    importlib.import_module("tools.lib.target_context")


def test_resolve_server_context_supports_ssh_key_and_runtime_defaults(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    _write_auth(
        paths,
        {
            "version": 1,
            "ssh_auth": {
                "refs": {
                    "default-server-auth": {
                        "kind": "ssh-key",
                        "username": "root",
                        "key_path": "/tmp/id_ed25519",
                    }
                }
            },
            "git_auth": {"refs": {}},
        },
    )
    _write_servers(
        paths,
        {
            "version": 1,
            "servers": {
                "lab-a": {
                    "host": "10.0.0.12",
                    "port": 22,
                    "login_user": "root",
                    "ssh_auth_ref": "default-server-auth",
                    "runtime": {
                        "image_ref": "image",
                        "container_name": "container",
                        "ssh_port": 41001,
                        "bootstrap_mode": "host-then-container",
                    },
                }
            },
        },
    )

    target_context = importlib.import_module("tools.lib.target_context")
    ctx = target_context.resolve_server_context(paths, "lab-a")

    assert ctx.host.host == "10.0.0.12"
    assert ctx.credential.mode == "ssh-key"
    assert ctx.credential.key_path == "/tmp/id_ed25519"
    assert ctx.runtime.workspace_root == "/vllm-workspace"
    assert ctx.runtime.host_workspace_path == "/root/.vaws/targets/lab-a/workspace"


def test_resolve_server_context_supports_password_auth(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    _write_auth(
        paths,
        {
            "version": 1,
            "ssh_auth": {
                "refs": {
                    "pw-auth": {
                        "kind": "password",
                        "username": "root",
                        "password_env": "SERVER_PASSWORD",
                    }
                }
            },
            "git_auth": {"refs": {}},
        },
    )
    _write_servers(
        paths,
        {
            "version": 1,
            "servers": {
                "lab-a": {
                    "host": "10.0.0.12",
                    "port": 22,
                    "login_user": "root",
                    "ssh_auth_ref": "pw-auth",
                    "runtime": {
                        "image_ref": "image",
                        "container_name": "container",
                        "ssh_port": 41001,
                        "bootstrap_mode": "host-then-container",
                    },
                }
            },
        },
    )

    target_context = importlib.import_module("tools.lib.target_context")
    ctx = target_context.resolve_server_context(paths, "lab-a")

    assert ctx.credential.mode == "password"
    assert ctx.credential.password_env == "SERVER_PASSWORD"


def test_resolve_server_context_rejects_local_simulation(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    _write_auth(
        paths,
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
    )
    _write_servers(
        paths,
        {
            "version": 1,
            "servers": {
                "lab-a": {
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
    )

    target_context = importlib.import_module("tools.lib.target_context")
    with pytest.raises(RuntimeError, match="local-simulation"):
        target_context.resolve_server_context(paths, "lab-a")


def test_list_managed_server_names_uses_target_context_module(vaws_repo):
    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    _write_auth(
        paths,
        {
            "version": 1,
            "ssh_auth": {"refs": {}},
            "git_auth": {"refs": {}},
        },
    )
    _write_servers(
        paths,
        {
            "version": 1,
            "servers": {
                "lab-b": {"host": "10.0.0.13", "port": 22, "login_user": "root", "runtime": {}},
                "lab-a": {"host": "10.0.0.12", "port": 22, "login_user": "root", "runtime": {}},
            },
        },
    )

    target_context = importlib.import_module("tools.lib.target_context")
    assert target_context.list_managed_server_names(paths) == ["lab-a", "lab-b"]
