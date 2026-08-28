from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import resolve_inventory_read_path, shared_inventory_path, shared_workspace_root
from vaws_remote_toolbox import _load_inventory
from vaws_session_state import sessions_root


class SharedInventoryStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.primary = Path(self.temp.name) / "primary"
        self.linked = Path(self.temp.name) / "linked"
        subprocess.run(["git", "init", str(self.primary)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.primary), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.primary), "config", "user.name", "Test"], check=True)
        (self.primary / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.primary), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.primary), "commit", "-m", "init"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["git", "-C", str(self.primary), "worktree", "add", "--detach", str(self.linked)],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_all_worktrees_resolve_one_primary_inventory(self) -> None:
        expected = self.primary / ".vaws-local" / "machine-inventory.json"
        self.assertEqual(shared_workspace_root(self.primary), self.primary.resolve())
        self.assertEqual(shared_workspace_root(self.linked), self.primary.resolve())
        self.assertEqual(shared_inventory_path(self.primary), expected.resolve())
        self.assertEqual(shared_inventory_path(self.linked), expected.resolve())

    def test_existing_linked_inventory_is_a_read_fallback_until_central_write(self) -> None:
        local_inventory = self.linked / ".vaws-local" / "machine-inventory.json"
        local_inventory.parent.mkdir(parents=True)
        local_inventory.write_text('{"schema_version":1,"machines":[]}\n', encoding="utf-8")
        preferred = shared_inventory_path(self.linked)
        self.assertEqual(
            resolve_inventory_read_path(preferred, repo_root=self.linked),
            local_inventory.resolve(),
        )

        preferred.parent.mkdir(parents=True)
        preferred.write_text('{"schema_version":1,"machines":[]}\n', encoding="utf-8")
        self.assertEqual(resolve_inventory_read_path(preferred, repo_root=self.linked), preferred.resolve())

    def test_toolbox_shares_inventory_but_sessions_remain_worktree_local(self) -> None:
        inventory_path = shared_inventory_path(self.primary)
        inventory_path.parent.mkdir(parents=True)
        inventory = {"schema_version": 1, "machines": [{"alias": "a3"}]}
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

        loaded, loaded_path = _load_inventory(self.linked)
        self.assertEqual(loaded, inventory)
        self.assertEqual(loaded_path, inventory_path.resolve())
        self.assertNotEqual(sessions_root(self.primary).resolve(), sessions_root(self.linked).resolve())
        self.assertEqual(sessions_root(self.linked), self.linked / ".vaws-local" / "sessions")


if __name__ == "__main__":
    unittest.main()
