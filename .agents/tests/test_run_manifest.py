#!/usr/bin/env python3
"""Tests for Run Manifest v1 and shared knowledge validation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_knowledge_v2 as v2  # noqa: E402
from vaws_run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)

NOW = "2026-07-25T12:00:00Z"


class RunManifestTests(unittest.TestCase):
    def test_round_trip_and_status_transition(self) -> None:
        manifest = new_manifest(
            run_type="correctness",
            run_id="correctness-case-1",
            workspace_snapshot={"workspace": "abc123", "dirty": False},
            command=["python", "run.py"],
            created_at=NOW,
        )
        running = transition_status(manifest, "running", updated_at=NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest(path, running)
            self.assertEqual(load_manifest(path), running)

    def test_invalid_status_transition_is_rejected(self) -> None:
        manifest = new_manifest(
            run_type="debug", run_id="debug-case-1", created_at=NOW
        )
        with self.assertRaises(RunManifestError):
            transition_status(manifest, "passed", updated_at=NOW)

    def test_secret_like_environment_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(RunManifestError, "secret-like key"):
            new_manifest(
                run_type="profiling",
                run_id="profile-case-1",
                environment_variables={"SERVICE_API_TOKEN": "do-not-store"},
                created_at=NOW,
            )

    def test_duplicate_artifact_name_is_rejected(self) -> None:
        manifest = new_manifest(
            run_type="performance", run_id="perf-case-1", created_at=NOW
        )
        manifest = add_artifact(
            manifest,
            name="report",
            kind="report",
            uri="report.md",
            updated_at=NOW,
        )
        with self.assertRaisesRegex(RunManifestError, "duplicated"):
            add_artifact(
                manifest,
                name="report",
                kind="raw",
                uri="raw.json",
                updated_at=NOW,
            )


class KnowledgeValidationTests(unittest.TestCase):
    def test_repository_knowledge_files_are_valid(self) -> None:
        knowledge_dir = ROOT / ".agents" / "knowledge"
        names = {path.name for path, _kind in v2.iter_documents(knowledge_dir)}
        self.assertTrue(names)
        self.assertTrue(all(name.endswith(".v2.yaml") for name in names))
        extras = {
            path.name
            for path in knowledge_dir.iterdir()
            if path.is_file() and not path.name.endswith(".v2.yaml")
        }
        self.assertEqual(extras, set())
        entries, problems = v2.load_entries(knowledge_dir)
        self.assertEqual(problems, [])
        self.assertGreaterEqual(len(entries), 1)

    def test_unknown_support_is_not_implicitly_valid(self) -> None:
        document = v2.new_document("model-capabilities", now="2026-07-25")
        document["entries"] = [
            {
                "uuid": v2.derived_uuid("test", "model-capabilities", "bad-entry"),
                "slug": "bad-entry",
                "content_hash": "sha256:" + "0" * 64,
                "status": "unknown",
                "confidence": "low",
                "scope": {},
                "provenance": {
                    "contributor": "test",
                    "origin_repo": "test/test",
                    "submitted_at": "2026-07-25",
                    "redaction_profile": "r2",
                },
                "lifecycle": {
                    "first_seen": "2026-07-25",
                    "updated_at": "2026-07-25",
                    "superseded_by": None,
                    "resolved_by": None,
                },
                "rule": {"summary": "x", "symptom": "x", "root_cause": "x", "resolution": "x"},
            }
        ]
        with self.assertRaises(v2.KnowledgeV2Error):
            v2.validate_document(
                document, expected_kind="model-capabilities", path="test.v2.yaml"
            )


if __name__ == "__main__":
    unittest.main()
