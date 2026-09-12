"""Stable native Cursor MCP configuration preserves user-owned providers."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_cursor_mcp_config import add_cursor_global_mcp, PIN_ENV


def server():
    return {"command": "/selected/bin/python", "args": ["-m", "vaws_coordinator", "task-server"],
            "env": {PIN_ENV: "/selected/receipt.json", "CUSTOM": "preserved",
                    "REMOTE_DEV_STATE_DIR": "/source/state", "VAWS_KNOWLEDGE_CONFIG": "/source/knowledge.json"},
            "timeout": 600000}


def fixture(tmp_path):
    root = tmp_path / "project 用户"
    root.mkdir()
    shared = tmp_path / "user/mcp.json"
    path = root / ".cursor/mcp.json"
    files = {path: json.dumps({"mcpServers": {"vaws-task": server(), "custom": {"command": "mine"}},
                              "customField": True})}
    return root, shared, path, files


def owned(value, _):
    return value.get("command") == "/selected/bin/python"


def test_explicit_setup_moves_only_generated_provider_and_preserves_global_fields(tmp_path):
    root, shared, path, files = fixture(tmp_path)
    files[shared] = json.dumps({"mcpServers": {"other": {"command": "keep"}}, "globalField": 12})
    add_cursor_global_mcp(files, [], root, root, owned_server=owned, enable=True, global_path=shared)
    result = json.loads(files[shared])
    value = result["mcpServers"]["vaws-task"]
    assert value["args"] == [str(root / ".agents/scripts/vaws_native_mcp.py"), "task"]
    assert value["env"] == {"CUSTOM": "preserved", "VAWS_MCP_WORKSPACE": "${workspaceFolder}"}
    assert result["globalField"] == 12
    assert result["mcpServers"]["other"] == {"command": "keep"}
    assert json.loads(files[path]) == {"mcpServers": {"custom": {"command": "mine"}}, "customField": True}


def test_default_setup_does_not_write_global_preferences(tmp_path):
    root, shared, path, files = fixture(tmp_path)
    before = dict(files)
    add_cursor_global_mcp(files, [], root, root, owned_server=owned, global_path=shared)
    assert files == before


def test_custom_same_name_global_is_preserved_and_keeps_project_provider(tmp_path):
    root, shared, path, files = fixture(tmp_path)
    files[shared] = json.dumps({"mcpServers": {"vaws-task": {"command": "custom-owner"}}})
    before = dict(files)
    notes = []
    add_cursor_global_mcp(files, notes, root, root, owned_server=owned, enable=True, global_path=shared)
    assert files == before
    assert notes[0]["reason"] == "custom-global-mcp-server"


def test_custom_same_name_project_provider_is_preserved(tmp_path):
    root, shared, path, files = fixture(tmp_path)
    files[path] = json.dumps({"mcpServers": {"vaws-task": {"command": "custom-owner"}}})
    before = dict(files)
    add_cursor_global_mcp(files, [], root, root, owned_server=owned, enable=True, global_path=shared)
    assert files == before


def test_repair_reuses_selected_global_entry_without_rewriting_its_environment(tmp_path):
    root, shared, path, files = fixture(tmp_path)
    add_cursor_global_mcp(files, [], root, root, owned_server=owned, enable=True, global_path=shared)
    global_text = files[shared]
    files[path] = json.dumps({"mcpServers": {"vaws-task": server()}})
    add_cursor_global_mcp(files, [], root, root, owned_server=owned, global_path=shared)
    assert json.loads(files[path])["mcpServers"] == {}
    assert files[shared] == global_text


def test_linked_worktree_drops_duplicate_without_retargeting_shared_entry(tmp_path):
    root, shared, path, files = fixture(tmp_path)
    def git(cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    git(root, "init")
    (root / "README").write_text("fixture")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    target = tmp_path / "native-worktree"
    git(root, "worktree", "add", "--detach", str(target), "HEAD")
    add_cursor_global_mcp(files, [], root, root, owned_server=owned, enable=True, global_path=shared)
    global_text = files[shared]
    target_path = target / ".cursor/mcp.json"
    files[target_path] = json.dumps({"mcpServers": {"vaws-task": server()}})
    add_cursor_global_mcp(files, [], target, target, owned_server=owned, global_path=shared)
    assert json.loads(files[target_path])["mcpServers"] == {}
    assert files[shared] == global_text
