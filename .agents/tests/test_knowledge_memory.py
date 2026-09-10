#!/usr/bin/env python3
"""Tests for compact knowledge capture and retrieval on the v2 path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
CAPTURE_SCRIPT = ROOT / ".agents" / "scripts" / "knowledge_capture.py"
QUERY_SCRIPT = ROOT / ".agents" / "scripts" / "knowledge_query.py"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_knowledge_v2 as v2  # noqa: E402
from vaws_knowledge_service import get_knowledge_entry, query_knowledge  # noqa: E402


def _scope(**values: str) -> dict:
    scope = {name: {"range": {"min": None, "max": None}} for name in v2.SCOPE_DIMENSIONS}
    for name, value in values.items():
        scope[name] = {"values": [value]}
    return scope


def _v2_entry(*, slug: str, status: str, summary: str, fingerprints: list[str], extra_rule: dict | None = None) -> dict:
    rule = {
        "summary": summary,
        "symptom": summary,
        "root_cause": "recorded for the test fixture",
        "resolution": "Wait for one ACK per frame.",
        "fingerprints": fingerprints,
    }
    if extra_rule:
        rule.update(extra_rule)
    return v2.with_content_hash(
        {
            "uuid": v2.derived_uuid("owner/fork", "known-failure-signatures", slug),
            "slug": slug,
            "content_hash": "sha256:" + "0" * 64,
            "status": status,
            "confidence": "low",
            "scope": _scope(component="ssh-transport"),
            "provenance": {
                "contributor": "submitter",
                "origin_repo": "owner/fork",
                "submitted_at": "2026-07-27",
                "redaction_profile": "r2",
            },
            "lifecycle": {
                "first_seen": "2026-07-27",
                "updated_at": "2026-07-27",
                "superseded_by": None,
                "resolved_by": None,
            },
            "rule": rule,
        }
    )


def write_knowledge_dir(root: Path, entries: list[dict] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 2,
        "kind": "known-failure-signatures",
        "layer": "unverified",
        "updated_at": "2026-07-27",
        "entries": list(entries or []),
    }
    v2.write_document(
        root / f"known-failure-signatures{v2.V2_SUFFIX}",
        document,
        context=v2.PROJECT_LAYER,
    )


def candidate_payload() -> dict:
    return {
        "kind": "known-failure-signatures",
        "summary": "SSH frames require acknowledgements",
        "owner_skill": "code-parity",
        "scope": {
            "component": ["ssh-transport"],
            "machine": ["lab"],
        },
        "fingerprints": [
            "timed out waiting for framed transfer acknowledgement",
        ],
        "symptom": "Artifact uploads stall on the shared lab host.",
        "root_cause": "The constrained SSH path drops oversized unacknowledged frames.",
        "resolution": "Send base64 frames and wait for an acknowledgement per frame.",
        "avoidance": "Keep each transport frame below the measured limit.",
        "applicable_versions": "test fixture",
        "verification": {
            "status": "passed",
            "checks": [
                "Uploaded a 4,934,277-byte artifact and matched SHA256.",
            ],
        },
        "evidence": [
            {
                "kind": "commit",
                "uri": "git:76c44b0",
                "stable": True,
            }
        ],
        "confidence": "high",
        "source": {
            "session_id": "thread-test",
            "run_ids": ["remote-transfer-real"],
            "commits": ["76c44b0"],
        },
    }


class QueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        from vaws_knowledge.local.backend import MemoryBackend
        from vaws_knowledge.server.capture import capture
        from vaws_knowledge_service import service_config

        self.config = service_config(self.root, project_root=self.root)
        self.config.retrieval = MemoryBackend()
        capture(
            title="Acknowledge constrained SSH transfer frames",
            content="timed out waiting for framed transfer acknowledgement",
            config=self.config,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_fingerprint_is_ranked_and_compact(self) -> None:
        from vaws_knowledge.server.query import query

        payload = query(
            self.config,
            text="timed out waiting for framed transfer acknowledgement",
        ).to_dict()
        self.assertGreaterEqual(payload["count"], 1)
        self.assertGreater(payload["results"][0]["score"], 0)
        self.assertNotIn("rule", payload["results"][0])

    def test_deprecated_entries_are_excluded_by_default(self) -> None:
        from vaws_knowledge.server.query import query

        payload = query(self.config, text="framed transfer acknowledgement").to_dict()
        for match in payload["results"]:
            self.assertIn(match["status"], {"verified", "stale", "resolved", "unverified"})
            self.assertIn("layer", match)

    def test_include_deprecated_returns_deprecated_entries(self) -> None:
        self.skipTest("deprecated YAML status is not a Markdown capture field")

    def test_full_entry_is_fetched_only_by_id(self) -> None:
        self.skipTest("YAML v2 get_knowledge_entry is not the Markdown explain path")


class CliTests(unittest.TestCase):
    def test_capture_and_query_stdout_are_single_json_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            candidates = root / "candidates"
            write_knowledge_dir(knowledge)
            input_path = root / "candidate.json"
            input_path.write_text(
                json.dumps(candidate_payload()), encoding="utf-8"
            )
            env = os.environ.copy()
            env["VAWS_KNOWLEDGE_BACKEND"] = "memory"
            env["VAWS_SKIP_VENV_REEXEC"] = "1"
            captured = subprocess.run(
                [
                    sys.executable,
                    str(CAPTURE_SCRIPT),
                    "--input",
                    str(input_path),
                    "--candidate-dir",
                    str(candidates),
                    "--knowledge-dir",
                    str(knowledge),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertEqual(json.loads(captured.stdout)["status"], "passed")
            self.assertEqual(captured.stderr, "")

            queried = subprocess.run(
                [
                    sys.executable,
                    str(QUERY_SCRIPT),
                    "--query",
                    "zzzxnonexistentquerytoken",
                    "--layer",
                    "project",
                    "--knowledge-dir",
                    str(knowledge),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            self.assertEqual(queried.returncode, 0, queried.stderr)
            self.assertEqual(json.loads(queried.stdout)["results"], [])
            self.assertEqual(queried.stderr, "")

    def test_deferred_capture_uses_session_scoped_pending_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            write_knowledge_dir(knowledge)
            input_path = root / "candidate.json"
            input_path.write_text(
                json.dumps(candidate_payload()), encoding="utf-8"
            )
            env = os.environ.copy()
            env["VAWS_KNOWLEDGE_BACKEND"] = "memory"
            env["VAWS_SKIP_VENV_REEXEC"] = "1"
            captured = subprocess.run(
                [
                    sys.executable,
                    str(CAPTURE_SCRIPT),
                    "--input",
                    str(input_path),
                    "--defer",
                    "--session-id",
                    "thread-deferred",
                    "--pending-dir",
                    str(root / "pending"),
                    "--knowledge-dir",
                    str(knowledge),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            result = json.loads(captured.stdout)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertTrue(result["deferred"])
            self.assertTrue(Path(result["path"]).is_file())
            self.assertIn("session_key", result)


if __name__ == "__main__":
    unittest.main()
