from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import agent_sessions_root, shared_inventory_path, shared_workspace_root


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

    def test_stale_linked_inventory_never_masks_missing_shared_inventory(self) -> None:
        local_inventory = self.linked / ".vaws-local" / "machine-inventory.json"
        local_inventory.parent.mkdir(parents=True)
        local_inventory.write_text('{"schema_version":1,"machines":[]}\n', encoding="utf-8")
        preferred = shared_inventory_path(self.linked)
        self.assertEqual(preferred, self.primary / ".vaws-local" / "machine-inventory.json")
        self.assertFalse(preferred.exists())

        preferred.parent.mkdir(parents=True)
        preferred.write_text('{"schema_version":1,"machines":[]}\n', encoding="utf-8")
        self.assertEqual(shared_inventory_path(self.linked), preferred)

    def test_independent_clone_does_not_inherit_primary_inventory(self) -> None:
        clone = Path(self.temp.name) / "clone"
        subprocess.run(["git", "clone", str(self.primary), str(clone)], check=True, capture_output=True)
        self.assertEqual(shared_workspace_root(clone), clone.resolve())
        self.assertNotEqual(shared_inventory_path(clone), shared_inventory_path(self.primary))

    def test_linked_worktree_uses_primary_attachment_registry(self) -> None:
        shared = self.primary / ".vaws-local" / "agent-sessions"
        self.assertEqual(agent_sessions_root(self.primary), shared)
        self.assertEqual(agent_sessions_root(self.linked), shared)


if __name__ == "__main__":
    unittest.main()
