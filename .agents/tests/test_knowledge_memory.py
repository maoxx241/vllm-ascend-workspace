#!/usr/bin/env python3
"""Tests for compact knowledge capture and retrieval."""

from __future__ import annotations

import json
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

from vaws_knowledge import (  # noqa: E402
    KNOWLEDGE_FILES,
    KnowledgeError,
    capture_candidate,
    get_knowledge_entry,
    normalize_fingerprint,
    normalize_candidate,
    query_knowledge,
)

NOW = "2026-07-27T12:00:00Z"


def write_knowledge_dir(root: Path, entries: dict[str, list[dict]] | None = None) -> None:
    entries = entries or {}
    root.mkdir(parents=True, exist_ok=True)
    for filename, kind in KNOWLEDGE_FILES.items():
        (root / filename).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": kind,
                    "updated_at": "2026-07-27",
                    "entries": entries.get(kind, []),
                }
            ),
            encoding="utf-8",
        )


def candidate_payload() -> dict:
    return {
        "kind": "known-failure-signatures",
        "summary": "Remote SSH frames require acknowledgements",
        "owner_skill": "remote-code-parity",
        "scope": {
            "component": ["ssh-transport"],
            "machine": ["hvv-sz"],
        },
        "fingerprints": [
            "timed out waiting for framed transfer acknowledgement",
        ],
        "symptom": "Artifact uploads stall on hvv-sz.",
        "root_cause": "The constrained SSH path drops oversized unacknowledged frames.",
        "resolution": "Send base64 frames and wait for an acknowledgement per frame.",
        "avoidance": "Keep each transport frame below the measured limit.",
        "applicable_versions": "workspace revisions before and including 76c44b0",
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


class CandidateTests(unittest.TestCase):
    def test_candidate_defaults_are_deterministic_and_valid(self) -> None:
        first = normalize_candidate(candidate_payload(), now=NOW)
        second = normalize_candidate(candidate_payload(), now=NOW)
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(first["status"], "candidate")
        self.assertEqual(first["occurrence_count"], 1)

    def test_secret_like_values_are_rejected(self) -> None:
        payload = candidate_payload()
        payload["resolution"] = "Use token sk-abcdefghijklmnopqrstuvwxyz123456"
        with self.assertRaisesRegex(KnowledgeError, "secret-like value"):
            normalize_candidate(payload, now=NOW)

    def test_absolute_evidence_paths_are_rejected(self) -> None:
        payload = candidate_payload()
        payload["evidence"][0]["uri"] = "/tmp/private-run.json"
        with self.assertRaisesRegex(KnowledgeError, "repository-relative"):
            normalize_candidate(payload, now=NOW)

    def test_evidence_path_traversal_is_rejected(self) -> None:
        payload = candidate_payload()
        payload["evidence"][0]["uri"] = "../outside/run.json"
        with self.assertRaisesRegex(KnowledgeError, "must not traverse"):
            normalize_candidate(payload, now=NOW)

    def test_volatile_fingerprint_values_are_normalized(self) -> None:
        first = normalize_fingerprint(
            "2026-07-27T12:00:00Z PID 123 failed at 0x7ffdeadbeef"
        )
        second = normalize_fingerprint(
            "2026-07-28T13:01:02Z pid=987 failed at 0x7ffaabbccdd"
        )
        self.assertEqual(first, second)

    def test_repeat_capture_merges_evidence_and_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            candidates = root / "candidates"
            write_knowledge_dir(knowledge)
            first = capture_candidate(
                candidate_payload(),
                candidate_dir=candidates,
                knowledge_dir=knowledge,
                now=NOW,
            )
            payload = candidate_payload()
            payload["evidence"].append(
                {"kind": "test", "uri": ".agents/tests/test_transport.py", "stable": True}
            )
            second = capture_candidate(
                payload,
                candidate_dir=candidates,
                knowledge_dir=knowledge,
                now="2026-07-27T13:00:00Z",
            )
            stored = json.loads(Path(second["path"]).read_text(encoding="utf-8"))
            self.assertEqual(first["action"], "created")
            self.assertEqual(second["action"], "updated")
            self.assertEqual(stored["occurrence_count"], 2)
            self.assertEqual(len(stored["evidence"]), 2)

    def test_identical_capture_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            candidates = root / "candidates"
            write_knowledge_dir(knowledge)
            capture_candidate(
                candidate_payload(),
                candidate_dir=candidates,
                knowledge_dir=knowledge,
                now=NOW,
            )
            second = capture_candidate(
                candidate_payload(),
                candidate_dir=candidates,
                knowledge_dir=knowledge,
                now="2026-07-27T13:00:00Z",
            )
            stored = json.loads(Path(second["path"]).read_text(encoding="utf-8"))
            self.assertEqual(second["action"], "unchanged")
            self.assertEqual(stored["occurrence_count"], 1)

    def test_promoted_candidate_is_not_written_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = normalize_candidate(candidate_payload(), now=NOW)
            entry = {
                "id": "remote-framed-transfer",
                "source": "verified real-machine run",
                "applicable_versions": "all",
                "updated_at": "2026-07-27",
                "status": "active",
                "rule": {"candidate_id": normalized["candidate_id"]},
            }
            knowledge = root / "knowledge"
            write_knowledge_dir(
                knowledge, {"known-failure-signatures": [entry]}
            )
            result = capture_candidate(
                candidate_payload(),
                candidate_dir=root / "candidates",
                knowledge_dir=knowledge,
                now=NOW,
            )
            self.assertEqual(result["status"], "already-promoted")
            self.assertFalse((root / "candidates").exists())


class QueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        active = {
            "id": "remote-framed-transfer",
            "source": "real hvv-sz validation",
            "applicable_versions": "all",
            "updated_at": "2026-07-27",
            "status": "active",
            "rule": {
                "summary": "Acknowledge constrained SSH transfer frames",
                "fingerprints": [
                    "timed out waiting for framed transfer acknowledgement"
                ],
                "root_cause": "The SSH path drops oversized frames.",
                "resolution": "Wait for one ACK per frame.",
            },
        }
        deprecated = {
            "id": "legacy-transfer",
            "source": "old run",
            "applicable_versions": "old",
            "updated_at": "2026-07-27",
            "status": "deprecated",
            "rule": {
                "summary": "Legacy transfer timeout",
                "fingerprints": ["transfer timeout"],
            },
        }
        write_knowledge_dir(
            self.root,
            {"known-failure-signatures": [active, deprecated]},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_fingerprint_is_ranked_and_compact(self) -> None:
        matches = query_knowledge(
            knowledge_dir=self.root,
            query="timed out waiting for framed transfer acknowledgement",
        )
        self.assertEqual(matches[0]["id"], "remote-framed-transfer")
        self.assertGreaterEqual(matches[0]["score"], 100)
        self.assertNotIn("rule", matches[0])

    def test_deprecated_entries_are_excluded_by_default(self) -> None:
        matches = query_knowledge(
            knowledge_dir=self.root, query="legacy transfer timeout"
        )
        self.assertNotIn("legacy-transfer", {match["id"] for match in matches})

    def test_full_entry_is_fetched_only_by_id(self) -> None:
        result = get_knowledge_entry(
            knowledge_dir=self.root, entry_id="remote-framed-transfer"
        )
        self.assertIsNotNone(result)
        self.assertIn("rule", result["entry"])


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
            )
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertEqual(json.loads(captured.stdout)["status"], "passed")
            self.assertEqual(captured.stderr, "")

            queried = subprocess.run(
                [
                    sys.executable,
                    str(QUERY_SCRIPT),
                    "--query",
                    "not present",
                    "--knowledge-dir",
                    str(knowledge),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(queried.returncode, 0, queried.stderr)
            self.assertEqual(json.loads(queried.stdout)["matches"], [])
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
            )
            result = json.loads(captured.stdout)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertTrue(result["deferred"])
            self.assertIn(result["session_key"], result["path"])


if __name__ == "__main__":
    unittest.main()
