#!/usr/bin/env python3
"""Machine-management skill contract. No host or container mutation."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "machine-management"


class MachineManagementTests(unittest.TestCase):
    def test_repair_and_remove_are_deleted(self) -> None:
        scripts = SKILL / "scripts"
        self.assertFalse((scripts / "machine_repair.py").is_file())
        self.assertFalse((scripts / "machine_remove.py").is_file())
        self.assertTrue((scripts / "machine_add.py").is_file())
        self.assertTrue((scripts / "machine_verify.py").is_file())

    def test_add_next_steps_name_the_published_provision_command(self) -> None:
        text = (SKILL / "scripts" / "machine_add.py").read_text(encoding="utf-8")
        self.assertIn(
            "python -m vaws_coordinator provision --host <host> --image <image> --user <user>",
            text,
        )
        self.assertNotIn("after the package publishes", text)

    def test_skill_docs_point_at_provision_not_tombstones(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("python -m vaws_coordinator provision --host", skill)
        self.assertNotIn("`machine_repair.py` and `machine_remove.py` refuse", skill)
        recipes = (SKILL / "references" / "command-recipes.md").read_text(encoding="utf-8")
        self.assertIn("python -m vaws_coordinator provision --host", recipes)


if __name__ == "__main__":
    unittest.main()
