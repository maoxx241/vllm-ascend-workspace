#!/usr/bin/env python3
"""End-to-end test for the public workspace knowledge lifecycle."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / ".agents" / "scripts" / "knowledge_capture.py"
QUERY = ROOT / ".agents" / "scripts" / "knowledge_query.py"
VALIDATE = ROOT / ".agents" / "scripts" / "knowledge_validate.py"
HOOK = ROOT / ".agents" / "hooks" / "knowledge_session_end.py"
CURATE = (
    ROOT
    / ".agents"
    / "skills"
    / "curate-workspace-knowledge"
    / "scripts"
    / "knowledge_curate.py"
)


def synthetic_candidate(session_id: str) -> dict:
    """Return realistic evidence without encoding project knowledge."""

    return {
        "kind": "known-failure-signatures",
        "summary": "Framed test transport requires acknowledgements",
        "owner_skill": "remote-code-parity",
        "scope": {"component": ["synthetic-transport"]},
        "fingerprints": ["synthetic framed transfer acknowledgement timeout"],
        "symptom": "The synthetic transfer stalls after its first frame.",
        "root_cause": "The test sender does not wait for the receiver acknowledgement.",
        "resolution": "Wait for one acknowledgement before sending the next test frame.",
        "avoidance": "Keep the synthetic transport acknowledgement-gated.",
        "applicable_versions": "test fixture only",
        "verification": {
            "status": "passed",
            "checks": ["The acknowledgement-gated regression test completed."],
        },
        "evidence": [
            {
                "kind": "regression-test",
                "uri": ".agents/tests/test_knowledge_flow.py",
                "stable": True,
            }
        ],
        "confidence": "high",
        "source": {
            "session_id": session_id,
            "run_ids": ["synthetic-knowledge-flow"],
            "commits": [],
        },
    }


class KnowledgeFlowE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temp.name)
        self.formal = self.sandbox / ".agents" / "knowledge"
        self.candidates = self.sandbox / ".vaws-local" / "knowledge" / "candidates"
        self.pending = self.sandbox / ".vaws-local" / "knowledge" / "pending"
        self.reviewed = self.sandbox / ".vaws-local" / "knowledge" / "reviewed"
        self.session_end = self.sandbox / ".vaws-local" / "knowledge" / "session-end"
        self.formal.parent.mkdir(parents=True)
        shutil.copytree(ROOT / ".agents" / "knowledge", self.formal)

        # The hook resolves the simulated repository from this marker but imports
        # the implementation under test from the real worktree.
        marker = self.sandbox / ".agents" / "lib" / "vaws_knowledge.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("# simulated repository marker\n", encoding="utf-8")

        self.source_knowledge = {
            path.name: path.read_bytes()
            for path in (ROOT / ".agents" / "knowledge").glob("*.yaml")
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_json(
        self, script: Path, *arguments: str, stdin: dict | None = None
    ) -> dict:
        completed = subprocess.run(
            [sys.executable, str(script), *arguments],
            input=json.dumps(stdin) if stdin is not None else None,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(completed.stdout.strip(), f"{script.name} emitted no JSON")
        return json.loads(completed.stdout)

    def curate_json(self, *arguments: str) -> dict:
        return self.run_json(
            CURATE,
            "--candidate-dir",
            str(self.candidates),
            "--reviewed-dir",
            str(self.reviewed),
            "--knowledge-dir",
            str(self.formal),
            *arguments,
        )

    def test_deferred_candidate_to_deprecated_formal_entry(self) -> None:
        session_id = "synthetic-knowledge-flow-session"
        input_path = self.sandbox / "candidate-input.json"
        input_path.write_text(
            json.dumps(synthetic_candidate(session_id)), encoding="utf-8"
        )

        deferred = self.run_json(
            CAPTURE,
            "--input",
            str(input_path),
            "--defer",
            "--session-id",
            session_id,
            "--pending-dir",
            str(self.pending),
            "--knowledge-dir",
            str(self.formal),
        )
        candidate_id = deferred["candidate_id"]
        self.assertTrue(deferred["deferred"])
        self.assertTrue(Path(deferred["path"]).is_file())

        hook_input = {
            "session_id": session_id,
            "transcript_path": "/not/read/by/the/hook.jsonl",
            "cwd": str(self.sandbox),
            "hook_event_name": "SessionEnd",
            "reason": "other",
        }
        hook = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(hook_input),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(hook.returncode, 0)
        self.assertEqual(hook.stdout, "")
        self.assertEqual(hook.stderr, "")
        self.assertTrue((self.candidates / f"{candidate_id}.json").is_file())
        receipts = list(self.session_end.glob("*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            json.loads(receipts[0].read_text(encoding="utf-8"))["status"], "passed"
        )

        listed = self.curate_json("list")
        self.assertEqual(
            [item["candidate_id"] for item in listed["candidates"]], [candidate_id]
        )
        inspected = self.curate_json("inspect", "--candidate-id", candidate_id)
        self.assertEqual(inspected["candidate"]["candidate_id"], candidate_id)
        self.assertEqual(inspected["possible_matches"], [])

        entry_id = "synthetic-framed-transfer"
        promoted = self.curate_json(
            "promote",
            "--candidate-id",
            candidate_id,
            "--entry-id",
            entry_id,
            "--status",
            "active",
        )
        self.assertEqual(promoted["action"], "promoted")
        self.assertEqual(promoted["entry_status"], "active")
        self.assertFalse((self.candidates / f"{candidate_id}.json").exists())
        self.assertTrue((self.reviewed / f"{candidate_id}.json").is_file())

        queried = self.run_json(
            QUERY,
            "--query",
            "synthetic framed transfer acknowledgement timeout",
            "--knowledge-dir",
            str(self.formal),
        )
        self.assertEqual(queried["matches"][0]["id"], entry_id)

        fetched = self.run_json(
            QUERY,
            "--id",
            entry_id,
            "--knowledge-dir",
            str(self.formal),
        )
        self.assertEqual(fetched["result"]["entry"]["status"], "active")

        recaptured = self.run_json(
            CAPTURE,
            "--input",
            str(input_path),
            "--candidate-dir",
            str(self.candidates),
            "--knowledge-dir",
            str(self.formal),
        )
        self.assertEqual(recaptured["status"], "already-promoted")
        self.assertEqual(list(self.candidates.glob("*.json")), [])

        deprecated = self.curate_json(
            "deprecate",
            "--entry-id",
            entry_id,
            "--reason",
            "Synthetic lifecycle completed.",
        )
        self.assertEqual(deprecated["action"], "deprecated")

        hidden = self.run_json(
            QUERY,
            "--query",
            "synthetic framed transfer acknowledgement timeout",
            "--knowledge-dir",
            str(self.formal),
        )
        self.assertEqual(hidden["matches"], [])
        retained = self.run_json(
            QUERY,
            "--query",
            "synthetic framed transfer acknowledgement timeout",
            "--include-deprecated",
            "--knowledge-dir",
            str(self.formal),
        )
        self.assertEqual(retained["matches"][0]["id"], entry_id)

        validated = self.run_json(
            VALIDATE, "--knowledge-dir", str(self.formal)
        )
        self.assertEqual(validated["status"], "passed")

        source_after = {
            path.name: path.read_bytes()
            for path in (ROOT / ".agents" / "knowledge").glob("*.yaml")
        }
        self.assertEqual(source_after, self.source_knowledge)


if __name__ == "__main__":
    unittest.main()
