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
from vaws_remote_toolbox import _load_inventory, RemoteToolboxError
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

    def test_stale_linked_inventory_never_masks_missing_shared_inventory(self) -> None:
        local_inventory = self.linked / ".vaws-local" / "machine-inventory.json"
        local_inventory.parent.mkdir(parents=True)
        local_inventory.write_text('{"schema_version":1,"machines":[]}\n', encoding="utf-8")
        preferred = shared_inventory_path(self.linked)
        self.assertEqual(
            resolve_inventory_read_path(preferred, repo_root=self.linked),
            preferred.resolve(),
        )
        with self.assertRaises(RemoteToolboxError):
            _load_inventory(self.linked)

        preferred.parent.mkdir(parents=True)
        preferred.write_text('{"schema_version":1,"machines":[]}\n', encoding="utf-8")
        self.assertEqual(resolve_inventory_read_path(preferred, repo_root=self.linked), preferred.resolve())

    def test_independent_clone_does_not_inherit_primary_inventory(self) -> None:
        clone = Path(self.temp.name) / "clone"
        subprocess.run(["git", "clone", str(self.primary), str(clone)], check=True, capture_output=True)
        self.assertEqual(shared_workspace_root(clone), clone.resolve())
        with self.assertRaises(RemoteToolboxError):
            _load_inventory(clone)

    def test_parity_machine_path_uses_same_shared_inventory(self) -> None:
        import importlib.util
        scripts = LIB_DIR.parent / "skills/remote-code-parity/scripts"
        sys.path.insert(0, str(scripts))
        spec = importlib.util.spec_from_file_location("shared_inventory_parity", scripts / "parity_sync.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        shared = shared_inventory_path(self.primary)
        shared.parent.mkdir(parents=True)
        inventory = {"schema_version": 1, "machines": [{"alias": "shared-a3"}]}
        shared.write_text(json.dumps(inventory))
        local = self.linked / ".vaws-local/machine-inventory.json"
        local.parent.mkdir(parents=True)
        local.write_text('{"machines":[{"alias":"stale"}]}')
        self.assertEqual(module.load_machine_inventory(self.linked), inventory)
        shared.unlink()
        with self.assertRaises(RuntimeError):
            module.load_machine_inventory(self.linked)

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
