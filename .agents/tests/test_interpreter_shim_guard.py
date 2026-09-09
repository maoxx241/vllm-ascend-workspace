#!/usr/bin/env python3
"""P9: local entries hop onto ``.venv``; remote payloads stay lib-free.

Test files are not entry points. A ``__main__`` file that is neither a test
nor in the CLI-surface inventory is a classification gap, not a file to shim.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY_SCRIPT = ROOT / ".agents" / "scripts" / "cli_surface_inventory.py"
LIB_DIR = ROOT / ".agents" / "lib"
SHIM_NAME = "ensure_workspace_interpreter"


def load_inventory_module():
    name = "_cli_surface_inventory_shim_guard"
    spec = importlib.util.spec_from_file_location(name, INVENTORY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inventory = load_inventory_module()
LIB_MODULES = frozenset(path.stem for path in LIB_DIR.glob("*.py") if path.stem != "__init__")


def _has_main(tree: ast.AST) -> bool:
    return inventory.has_main_guard(tree)


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue
            if node.module:
                names.add(node.module.split(".", 1)[0])
    return names


class InterpreterShimGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = inventory.build_inventory(ROOT, quiet=True)
        cls.entries = {item["path"]: item for item in cls.payload["entry_points"]}

    def test_inventoried_non_payloads_contain_the_shim(self) -> None:
        missing = []
        for path, record in sorted(self.entries.items()):
            role = (record.get("classification") or {}).get("support_role")
            if role == "payload":
                continue
            text = (ROOT / path).read_text(encoding="utf-8")
            if SHIM_NAME not in text:
                missing.append(path)
        self.assertEqual(missing, [])

    def test_payloads_import_nothing_from_agents_lib(self) -> None:
        leaked = []
        for path, record in sorted(self.entries.items()):
            role = (record.get("classification") or {}).get("support_role")
            if role != "payload":
                continue
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=path)
            imported = _imported_names(tree) & LIB_MODULES
            if imported:
                leaked.append(f"{path}: {sorted(imported)}")
        self.assertEqual(leaked, [])

    def test_main_files_that_are_neither_tests_nor_inventory_are_gaps(self) -> None:
        gaps = []
        for rel in inventory.tracked_files(ROOT):
            if not rel.startswith(".agents/") or not rel.endswith(".py"):
                continue
            if inventory.is_test_path(rel):
                continue
            try:
                tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
            except (SyntaxError, UnicodeDecodeError):
                continue
            if not _has_main(tree):
                continue
            if rel not in self.entries:
                gaps.append(rel)
        self.assertEqual(gaps, [])


if __name__ == "__main__":
    unittest.main()
