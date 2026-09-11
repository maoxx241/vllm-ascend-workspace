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

from vaws_coordinator.run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)

NOW = "2026-07-25T12:00:00Z"


class RunManifestTests(unittest.TestCase):
    def test_missing_identity_is_refused(self) -> None:
        with self.assertRaisesRegex(RunManifestError, "code identity is required"):
            new_manifest(
                run_type="debug",
                run_id="debug-case-1",
                created_at=NOW,
            )

    def test_workspace_root_resolves_real_identity(self) -> None:
        manifest = new_manifest(
            run_type="debug",
            run_id="debug-case-1",
            created_at=NOW,
            workspace_root=ROOT,
        )
        self.assertRegex(manifest["code"]["source_head"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(manifest["code"]["source_head"], "0" * 40)
        self.assertRegex(manifest["code"]["snapshot_commit"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(manifest["code"]["snapshot_commit"], "0" * 40)

    def test_round_trip_and_status_transition(self) -> None:
        manifest = new_manifest(
            run_type="correctness",
            run_id="correctness-case-1",
            workspace_snapshot={"workspace": "abc123", "dirty": False},
            command=["python", "run.py"],
            created_at=NOW,
            workspace_root=ROOT,
        )
        running = transition_status(manifest, "running", updated_at=NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest(path, running)
            self.assertEqual(load_manifest(path), running)

    def test_invalid_status_transition_is_rejected(self) -> None:
        manifest = new_manifest(
            run_type="debug",
            run_id="debug-case-1",
            created_at=NOW,
            workspace_root=ROOT,
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
                workspace_root=ROOT,
            )

    def test_duplicate_artifact_name_is_rejected(self) -> None:
        manifest = new_manifest(
            run_type="performance",
            run_id="perf-case-1",
            created_at=NOW,
            workspace_root=ROOT,
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



if __name__ == "__main__":
    unittest.main()
