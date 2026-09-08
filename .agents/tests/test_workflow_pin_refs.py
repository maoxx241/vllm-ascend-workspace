#!/usr/bin/env python3
"""Workflows must not duplicate a pin-file SHA.

Pin files under ``.agents/deps/`` are the only identity for the four external
checkouts. A literal 40-hex SHA in ``.github/workflows/`` that also appears in
a pin file is the duplication #103 left open: the two copies drift, and the
failure looks like a conformance-kit or checkout error.

This guard lives here rather than in ``tracked_path_check.py`` because that
tool is about dead in-tree paths in docs, not about SHA identity. A unittest
is the smallest place that makes reintroduction fail ``unittest discover``.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPS_DIR = ROOT / ".agents" / "deps"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def _pin_shas() -> set[str]:
    found: set[str] = set()
    for path in sorted(DEPS_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        found.update(SHA_RE.findall(text))
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        commit = data.get("commit") if isinstance(data, dict) else None
        if isinstance(commit, str) and SHA_RE.fullmatch(commit):
            found.add(commit)
    return found


def _workflow_pin_duplicates() -> list[tuple[str, str]]:
    pins = _pin_shas()
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


class WorkflowPinRefTests(unittest.TestCase):
    def test_no_workflow_repeats_a_pin_sha(self) -> None:
        hits = _workflow_pin_duplicates()
        self.assertEqual(
            hits,
            [],
            "workflow files must not contain a 40-hex SHA that also appears "
            "in .agents/deps/*.json; read the pin at job time instead",
        )

    def test_reintroducing_a_pin_sha_is_detected(self) -> None:
        pins = _pin_shas()
        self.assertTrue(pins)
        sample = next(iter(sorted(pins)))
        fake = f"ref: {sample}\n"
        self.assertIn(sample, SHA_RE.findall(fake))
        self.assertIn(sample, pins)


if __name__ == "__main__":
    unittest.main()
