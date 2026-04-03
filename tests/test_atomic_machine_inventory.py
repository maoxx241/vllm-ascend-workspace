from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.overlay import ensure_overlay_layout


def _family_tool_ids(family_name: str) -> list[str]:
    manifest_path = ROOT / f".agents/discovery/families/{family_name}.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    return [tool["tool_id"] for tool in manifest["tools"]]


def test_machine_inventory_family_lists_inventory_only_tools():
    assert _family_tool_ids("machine-inventory") == [
        "machine.register_server",
        "machine.describe_server",
        "machine.list_servers",
        "machine.remove_server",
    ]


def test_machine_register_server_mutates_only_servers_yaml(vaws_repo):
    from tools.atomic.machine_register_server import register_server

    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    auth_before = paths.local_auth_file.read_text(encoding="utf-8")
    repos_before = paths.local_repos_file.read_text(encoding="utf-8")

    result = register_server(
        paths,
        "lab-a",
        "10.0.0.12",
        ssh_auth_ref="default-server-auth",
    )

    servers = yaml.safe_load(paths.local_servers_file.read_text(encoding="utf-8"))
    assert result["status"] == "ready"
    assert paths.local_auth_file.read_text(encoding="utf-8") == auth_before
    assert paths.local_repos_file.read_text(encoding="utf-8") == repos_before
    assert "lab-a" in servers["servers"]
    assert result["side_effects"] == ["servers inventory updated"]


def test_machine_describe_and_remove_server_use_inventory_only(vaws_repo):
    from tools.atomic.machine_describe_server import describe_server
    from tools.atomic.machine_register_server import register_server
    from tools.atomic.machine_remove_server import remove_server

    paths = RepoPaths(root=vaws_repo)
    ensure_overlay_layout(paths)
    register_server(
        paths,
        "lab-a",
        "10.0.0.12",
        ssh_auth_ref="default-server-auth",
    )

    describe_result = describe_server(paths, "lab-a")
    remove_result = remove_server(paths, "lab-a")

    assert describe_result["status"] == "ready"
    assert describe_result["payload"]["server"]["host"] == "10.0.0.12"
    assert remove_result["status"] == "ready"
    assert "lab-a" not in yaml.safe_load(paths.local_servers_file.read_text(encoding="utf-8"))["servers"]
