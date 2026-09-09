#!/usr/bin/env python3
"""Entries that import installed packages must hop onto ``.venv``.

CI runs these scripts under ``uv run``, which hides a missing hop. This
test uses a system interpreter that cannot see the workspace packages.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = ("vaws_knowledge", "remote_dev", "vaws_coordinator")
WRAPPERS = ("vaws_knowledge_v2", "vaws_knowledge_service")
IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+("
    + "|".join(re.escape(name) for name in (*SENTINEL, *WRAPPERS))
    + r")\b",
    re.MULTILINE,
)


def _system_python() -> str:
    here = Path(sys.executable).resolve()
    for candidate in ("/usr/bin/python3", "/bin/python3"):
        path = Path(candidate)
        if path.is_file() and path.resolve() != here:
            return candidate
    return "/usr/bin/python3"


def _entry_roots() -> list[Path]:
    roots = [ROOT / ".agents" / "scripts", ROOT / ".agents" / "hooks"]
    skills = ROOT / ".agents" / "skills"
    if skills.is_dir():
        roots.extend(sorted(skills.glob("*/scripts")))
    return roots


def _is_entry(path: Path) -> bool:
    if path.name.startswith("_") or path.suffix != ".py":
        return False
    text = path.read_text(encoding="utf-8")
    return 'if __name__ == "__main__"' in text or "if __name__ == '__main__'" in text


def _imports_installed_package(path: Path) -> bool:
    return bool(IMPORT_RE.search(path.read_text(encoding="utf-8")))


def packaged_entries() -> list[Path]:
    found: list[Path] = []
    for root in _entry_roots():
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.py")):
            if _is_entry(path) and _imports_installed_package(path):
                found.append(path)
    return found


class VenvReexecEntryTests(unittest.TestCase):
    def test_packaged_entries_help_without_module_not_found(self) -> None:
        interpreter = _system_python()
        entries = packaged_entries()
        self.assertTrue(entries)
        curate = ROOT / ".agents" / "skills" / "curate-workspace-knowledge" / "scripts" / "knowledge_curate.py"
        self.assertIn(curate, entries)
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        failures: list[str] = []
        for path in entries:
            completed = subprocess.run(
                [interpreter, str(path), "--help"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            blob = completed.stdout + completed.stderr
            if "ModuleNotFoundError" in blob:
                failures.append(f"{path.relative_to(ROOT)}: {blob.strip().splitlines()[-1]}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
