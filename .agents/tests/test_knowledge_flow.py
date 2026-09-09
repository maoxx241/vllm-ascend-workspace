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
sys.path.insert(0, str(ROOT / ".agents" / "lib"))
from vaws_knowledge_service import get_knowledge_entry, query_knowledge  # noqa: E402

CAPTURE = ROOT / ".agents" / "scripts" / "knowledge_capture.py"
VALIDATE = ROOT / ".agents" / "scripts" / "knowledge_validate.py"
CURATE = (
    ROOT
    / ".agents"
    / "skills"
    / "curate-workspace-knowledge"
    / "scripts"
    / "knowledge_curate.py"
)


def synthetic_candidate(session_id: str) -> dict:
    """Return realistic evidence without encoding project knowledge.

    Vocabulary is deliberately nonce-based (``zqxjk``/``flovmar``/…): the
    inspect step asserts ``possible_matches == []``, and any real-store entry
    sharing common ops words (e.g. "timeout", "transfer") would otherwise
    score as a spurious token-overlap match as the store grows.
    """

    return {
        "kind": "known-failure-signatures",
        "summary": "Zqxjk flovmar blorpt requires acknowledgements",
        "owner_skill": "code-parity",
        "scope": {"component": ["synthetic-transport"]},
        "fingerprints": ["zqxjk flovmar blorpt acknowledgement nonce"],
        "symptom": "The zqxjk flovmar stalls after its first blorpt.",
        "root_cause": "The test sender does not wait for the receiver acknowledgement.",
        "resolution": "Wait for one acknowledgement before sending the next zqxjk frame.",
        "avoidance": "Keep the zqxjk flovmar acknowledgement-gated.",
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
        self.candidates = self.sandbox / ".vaws-local" / "knowledge" / "candidate"
        self.reviewed = self.sandbox / ".vaws-local" / "knowledge" / "reviewed"
        self.formal.parent.mkdir(parents=True)
        shutil.copytree(ROOT / ".agents" / "knowledge", self.formal)

        # The hook resolves the simulated repository from this marker but imports
        # the implementation under test from the real worktree.
        marker = self.sandbox / ".agents" / "lib" / "vaws_knowledge_service.py"
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
        """Capture, promote, query, and deprecate on the federated v2 path."""

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
            "--candidate-dir",
            str(self.candidates),
            "--knowledge-dir",
            str(self.formal),
        )
        candidate_id = deferred["candidate_id"]
        self.assertTrue(deferred["deferred"])
        self.assertTrue(Path(deferred["path"]).is_file())

        listed = self.curate_json("list")
        self.assertEqual(
            [item["candidate_id"] for item in listed["candidates"]], [candidate_id]
        )
        inspected = self.curate_json("inspect", "--candidate-id", candidate_id)
        self.assertEqual(inspected["candidate"]["candidate_id"], candidate_id)

        entry_id = candidate_id
        self.assertNotIn(
            "synthetic-framed-transfer",
            [match["id"] for match in inspected["possible_matches"]],
        )
        promoted = self.curate_json(
            "promote",
            "--candidate-id",
            candidate_id,
            "--status",
            "active",
        )
        self.assertEqual(promoted["action"], "promoted")
        self.assertEqual(promoted["entry_status"], "unverified")
        leftover = list(self.candidates.glob("*.yaml"))
        self.assertFalse(
            any(candidate_id in path.read_text(encoding="utf-8") for path in leftover)
        )

        queried = query_knowledge(
            knowledge_dir=self.formal,
            query="zqxjk flovmar blorpt acknowledgement nonce",
            include_unverified=True,
        )
        self.assertEqual(queried[0]["id"], entry_id)
        fetched = get_knowledge_entry(knowledge_dir=self.formal, entry_id=entry_id)
        self.assertEqual(fetched["entry"]["status"], "unverified")

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

        deprecated = self.curate_json(
            "deprecate",
            "--entry-id",
            entry_id,
            "--reason",
            "Synthetic lifecycle completed.",
        )
        self.assertEqual(deprecated["action"], "deprecated")

        hidden = query_knowledge(
            knowledge_dir=self.formal,
            query="zqxjk flovmar blorpt acknowledgement nonce",
        )
        self.assertNotIn(entry_id, [match["id"] for match in hidden])

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
