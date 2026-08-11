#!/usr/bin/env python3
"""Tests for the repository-local skill catalog validator."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents" / "scripts" / "skill_catalog.py"


def load_catalog_module():
    module_name = "_skill_catalog_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


catalog = load_catalog_module()


def write_minimal_repo(root: Path, *, docs_include: str = "example-skill") -> None:
    skill = root / ".agents" / "skills" / "example-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: example-skill\n"
        "description: Use for deterministic catalog tests.\n"
        "---\n\n"
        "# Example\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        f"| **{docs_include}** | purpose | when |\n", encoding="utf-8"
    )
    (root / "README.en.md").write_text(
        f"| **{docs_include}** | purpose | when |\n", encoding="utf-8"
    )
    (root / "AGENTS.md").write_text(
        f"| `{docs_include}` | purpose |\n", encoding="utf-8"
    )
    agents_readme = root / ".agents" / "README.md"
    agents_readme.parent.mkdir(exist_ok=True)
    agents_readme.write_text(
        f"- `.agents/skills/{docs_include}/` is canonical.\n", encoding="utf-8"
    )


class SkillCatalogTests(unittest.TestCase):
    def test_current_repository_catalog_is_complete(self) -> None:
        records, findings = catalog.validate_repo(ROOT)
        self.assertGreater(len(records), 0)
        self.assertEqual(findings, [])

    def test_directory_and_frontmatter_name_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_minimal_repo(root)
            skill_file = root / ".agents" / "skills" / "example-skill" / "SKILL.md"
            skill_file.write_text(
                skill_file.read_text(encoding="utf-8").replace(
                    "name: example-skill", "name: another-skill"
                ),
                encoding="utf-8",
            )
            _records, findings = catalog.validate_repo(root)
            self.assertIn("name-mismatch", {finding.code for finding in findings})

    def test_document_catalog_drift_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_minimal_repo(root, docs_include="stale-skill")
            _records, findings = catalog.validate_repo(root)
            codes = {finding.code for finding in findings}
            self.assertIn("catalog-entry-missing", codes)
            self.assertIn("catalog-entry-unknown", codes)

    def test_missing_relative_markdown_link_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_minimal_repo(root)
            skill_file = root / ".agents" / "skills" / "example-skill" / "SKILL.md"
            skill_file.write_text(
                skill_file.read_text(encoding="utf-8")
                + "\nSee [acceptance](references/acceptance.md).\n",
                encoding="utf-8",
            )
            _records, findings = catalog.validate_repo(root)
            self.assertIn("link-missing", {finding.code for finding in findings})

    def test_description_with_angle_brackets_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_minimal_repo(root)
            skill_file = root / ".agents" / "skills" / "example-skill" / "SKILL.md"
            skill_file.write_text(
                skill_file.read_text(encoding="utf-8").replace(
                    "Use for deterministic catalog tests.",
                    "Use when a local -> remote copy is required.",
                ),
                encoding="utf-8",
            )
            _records, findings = catalog.validate_repo(root)
            self.assertIn("description-invalid", {finding.code for finding in findings})


if __name__ == "__main__":
    unittest.main()
