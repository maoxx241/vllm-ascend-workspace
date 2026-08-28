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

from vaws_knowledge import (  # noqa: E402
    KNOWLEDGE_FILES,
    KnowledgeError,
    validate_knowledge_dir,
    validate_knowledge_document,
)
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
        files = validate_knowledge_dir(ROOT / ".agents" / "knowledge")
        self.assertEqual(set(files), set(KNOWLEDGE_FILES))

    def test_unknown_support_is_not_implicitly_valid(self) -> None:
        document = {
            "schema_version": 1,
            "kind": "model-capabilities",
            "updated_at": "2026-07-25",
            "entries": [
                {
                    "id": "bad-entry",
                    "source": "test",
                    "applicable_versions": "all",
                    "updated_at": "2026-07-25",
                    "status": "unknown",
                    "rule": {},
                }
            ],
        }
        with self.assertRaises(KnowledgeError):
            validate_knowledge_document(
                document, expected_kind="model-capabilities", path="test.yaml"
            )

    def test_json_compatible_yaml_requirement_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename, kind in KNOWLEDGE_FILES.items():
                (root / filename).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": kind,
                            "updated_at": "2026-07-25",
                            "entries": [],
                        }
                    ),
                    encoding="utf-8",
                )
            (root / "model-capabilities.yaml").write_text(
                "schema_version: 1\nkind: model-capabilities\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(KnowledgeError, "JSON-compatible YAML"):
                validate_knowledge_dir(root)


if __name__ == "__main__":
    unittest.main()
