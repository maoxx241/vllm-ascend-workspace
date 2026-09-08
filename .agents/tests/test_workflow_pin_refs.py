#!/usr/bin/env python3
"""Workflows must not duplicate a uv.lock commit SHA.

``uv.lock`` is the only pin. A literal 40-hex SHA in ``.github/workflows/``
that also appears in the lockfile is the same duplication the old pin files
had: the two copies drift.

This guard lives here rather than in ``tracked_path_check.py`` because that
tool is about dead in-tree paths in docs, not about SHA identity.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_dependency as deps  # noqa: E402


def _lock_shas() -> set[str]:
    found: set[str] = set()
    for row in deps.locked_packages().values():
        commit = row.get("commit")
        if isinstance(commit, str) and SHA_RE.fullmatch(commit):
            found.add(commit)
    return found


def _workflow_lock_duplicates() -> list[tuple[str, str]]:
    pins = _lock_shas()
    hits: list[tuple[str, str]] = []
    if not WORKFLOWS_DIR.is_dir():
        return hits
    for path in sorted(WORKFLOWS_DIR.iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        text = path.read_text(encoding="utf-8")
        for sha in SHA_RE.findall(text):
            if sha in pins:
                hits.append((path.as_posix(), sha))
    return hits


class WorkflowLockRefTests(unittest.TestCase):
    def test_no_workflow_repeats_a_lock_sha(self) -> None:
        hits = _workflow_lock_duplicates()
        self.assertEqual(
            hits,
            [],
            "workflow files must not contain a 40-hex SHA that also appears "
            "in uv.lock; use `uv sync --locked` instead",
        )

    def test_reintroducing_a_lock_sha_is_detected(self) -> None:
        pins = _lock_shas()
        self.assertTrue(pins)
        sample = next(iter(sorted(pins)))
        fake = f"ref: {sample}\n"
        self.assertIn(sample, SHA_RE.findall(fake))
        self.assertIn(sample, pins)

    def test_workflows_use_locked_sync(self) -> None:
        texts = []
        for path in sorted(WORKFLOWS_DIR.iterdir()):
            if path.suffix in {".yml", ".yaml"}:
                texts.append(path.read_text(encoding="utf-8"))
        blob = "\n".join(texts)
        self.assertIn("uv sync --locked", blob)


if __name__ == "__main__":
    unittest.main()
