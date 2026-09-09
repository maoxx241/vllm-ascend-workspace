#!/usr/bin/env python3
"""End-to-end test for the federated v2 knowledge lifecycle.

Capture with a real coordinate -> promote -> resolve -> verify -> export, plus
the three-layer query surface. Runs entirely on synthetic entries and needs no
NPU: everything under test is contract logic, not device behaviour.
"""

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

import vaws_knowledge_v2 as v2  # noqa: E402

CAPTURE = ROOT / ".agents" / "scripts" / "knowledge_capture.py"
QUERY = ROOT / ".agents" / "scripts" / "knowledge_query.py"
VALIDATE = ROOT / ".agents" / "scripts" / "knowledge_validate.py"
EXPORT = ROOT / ".agents" / "scripts" / "knowledge_export.py"
CURATE = (
    ROOT
    / ".agents"
    / "skills"
    / "curate-workspace-knowledge"
    / "scripts"
    / "knowledge_curate.py"
)

CONCRETE = {
    "soc": "Ascend910_93",
    "cann": "8.2.RC1",
    "driver": "25.0.rc1.1",
    "python_abi": "cp311",
    "torch": "2.7.1",
    "torch_npu": "2.7.1.dev20250724",
    "vllm": "0.11.0",
    "vllm_ascend": "0.11.0rc1",
}
FINGERPRINT = "zqxjk flovmar blorpt acknowledgement nonce"


def synthetic_candidate() -> dict:
    """Nonce vocabulary, so token overlap with the real corpus cannot match."""

    return {
        "kind": "known-failure-signatures",
        "summary": "Zqxjk flovmar blorpt requires acknowledgements",
        "owner_skill": "code-parity",
        "scope": {"component": ["synthetic-transport"]},
        "fingerprints": [FINGERPRINT],
        "symptom": "The zqxjk flovmar stalls after its first blorpt.",
        "root_cause": "The sender does not wait for the receiver acknowledgement.",
        "resolution": "Wait for one acknowledgement before sending the next frame.",
        "avoidance": "Keep the zqxjk flovmar acknowledgement-gated.",
        "verification": {
            "status": "passed",
            "checks": ["The acknowledgement-gated regression test completed."],
        },
        "evidence": [{"kind": "pr", "uri": "owner/repo#42", "stable": True}],
        "confidence": "high",
        "source": {
            "session_id": "synthetic-v2-flow",
            "run_ids": ["synthetic-v2-flow"],
            "commits": [],
        },
    }


class V2FlowBase(unittest.TestCase):
    """Sandbox plus script helpers shared by the lifecycle and query cases."""

    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temp.name)
        self.knowledge = self.sandbox / ".agents" / "knowledge"
        self.knowledge.parent.mkdir(parents=True)
        shutil.copytree(ROOT / ".agents" / "knowledge", self.knowledge)
        self.candidates = self.sandbox / ".vaws-local" / "knowledge" / "candidates"
        self.commons_candidates = self.sandbox / ".vaws-local" / "knowledge" / "candidate"
        self.reviewed = self.sandbox / ".vaws-local" / "knowledge" / "reviewed"
        self.export = self.sandbox / ".vaws-local" / "knowledge" / "export"
        self.input = self.sandbox / "candidate.json"
        self.input.write_text(json.dumps(synthetic_candidate()), encoding="utf-8")
        self.source_knowledge = {
            path.name: path.read_bytes()
            for path in (ROOT / ".agents" / "knowledge").glob("*.yaml")
        }

    def tearDown(self) -> None:
        # The real corpus must never be touched by a test run.
        after = {
            path.name: path.read_bytes()
            for path in (ROOT / ".agents" / "knowledge").glob("*.yaml")
        }
        self.assertEqual(after, self.source_knowledge)
        self.temp.cleanup()

    def run_script(self, script: Path, *arguments: str, expect: int = 0) -> dict:
        completed = subprocess.run(
            [sys.executable, str(script), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode, expect, completed.stdout + completed.stderr
        )
        self.assertTrue(completed.stdout.strip(), f"{script.name} emitted no JSON")
        return json.loads(completed.stdout)

    def curate(self, *arguments: str, expect: int = 0) -> dict:
        return self.run_script(
            CURATE,
            "--candidate-dir",
            str(self.commons_candidates),
            "--reviewed-dir",
            str(self.reviewed),
            "--knowledge-dir",
            str(self.knowledge),
            *arguments,
            expect=expect,
        )

    def query(self, *arguments: str) -> dict:
        return self.run_script(
            QUERY,
            "--knowledge-dir",
            str(self.knowledge),
            "--candidate-dir",
            str(self.commons_candidates),
            *arguments,
        )

    def capture(self, *arguments: str) -> dict:
        return self.run_script(
            CAPTURE,
            "--input",
            str(self.input),
            "--candidate-dir",
            str(self.commons_candidates),
            "--knowledge-dir",
            str(self.knowledge),
            *arguments,
        )

    def env_arguments(self, **overrides: str) -> list[str]:
        values = {**CONCRETE, **overrides}
        arguments: list[str] = []
        for name, value in values.items():
            arguments.extend(["--env", f"{name}={value}"])
        return arguments


class KnowledgeV2LifecycleTest(V2FlowBase):
    def test_capture_records_the_coordinate_and_names_the_unknowns(self) -> None:
        captured = self.capture(*self.env_arguments())
        self.assertEqual(captured["schema_version"], 2)
        self.assertFalse(captured["coordinate"]["complete"])
        self.assertEqual(
            captured["coordinate"]["unknown_dimensions"],
            ["model", "topology", "execution_mode"],
        )
        self.assertTrue(Path(captured["path"]).is_file())
        self.assertEqual(captured["coordinate"]["values"]["cann"], "8.2.RC1")
        # Nothing is invented for a dimension nobody established.
        self.assertEqual(captured["coordinate"]["values"]["model"], "unknown")

    def test_capture_without_a_run_context_falls_back_to_candidate_scope(self) -> None:
        captured = self.capture()
        # The candidate declares its own component; everything else is unknown
        # rather than inferred from the narrative.
        self.assertEqual(captured["coordinate"]["source"], "candidate-scope")
        self.assertNotIn("component", captured["coordinate"]["unknown_dimensions"])
        self.assertEqual(len(captured["coordinate"]["unknown_dimensions"]), 11)

    def test_capture_with_no_context_at_all_is_all_unknown(self) -> None:
        payload = synthetic_candidate()
        # scope is required by the candidate contract, but nothing in it names
        # a coordinate dimension.
        payload["scope"] = {"note": ["nothing coordinate-shaped here"]}
        self.input.write_text(json.dumps(payload), encoding="utf-8")
        captured = self.capture()
        self.assertEqual(len(captured["coordinate"]["unknown_dimensions"]), 12)
        self.assertEqual(captured["coordinate"]["source"], "unavailable")

    def test_full_promote_resolve_verify_export_lifecycle(self) -> None:
        candidate_id = self.capture(*self.env_arguments())["candidate_id"]

        promoted = self.curate(
            "promote",
            "--candidate-id",
            candidate_id,
            "--entry-id",
            "synthetic-ack-gate",
            "--origin-repo",
            "owner/fork",
            "--contributor",
            "submitter",
        )
        self.assertEqual(promoted["schema_version"], 2)
        # A local promotion is an observation, never a verified claim.
        self.assertEqual(promoted["entry_status"], "unverified")
        self.assertFalse(promoted["exportable"])
        self.assertEqual(
            [item["dimension"] for item in promoted["needs_human_input"]],
            ["model", "topology", "execution_mode"],
        )

        blocked = self.run_script(
            EXPORT,
            "--knowledge-dir",
            str(self.knowledge),
            "--export-dir",
            str(self.export),
            "--origin-repo",
            "owner/fork",
            "--entry",
            "synthetic-ack-gate",
            "--check",
            expect=1,
        )
        self.assertEqual(blocked["exportable"], [])
        self.assertEqual(
            blocked["blocked"][0]["unresolved_dimensions"],
            ["model", "topology", "execution_mode"],
        )

        listed = self.curate("list-unresolved")
        self.assertIn(
            "synthetic-ack-gate", [item["entry_id"] for item in listed["blocked"]]
        )

        self.curate(
            "resolve",
            "--entry-id",
            "synthetic-ack-gate",
            "--dimension",
            "topology",
            "--values",
            "tp2",
            "tp8",
        )
        self.curate(
            "resolve",
            "--entry-id",
            "synthetic-ack-gate",
            "--dimension",
            "execution_mode",
            "--values",
            "eager",
        )
        resolved = self.curate(
            "resolve",
            "--entry-id",
            "synthetic-ack-gate",
            "--dimension",
            "model",
            "--any-basis",
            "reproduced on two unrelated model families in one session",
        )
        self.assertEqual(resolved["unresolved_remaining"], [])

        # Verification needs a second party: a submitter cannot confirm itself.
        self_confirmed = self.curate(
            "verify",
            "--entry-id",
            "synthetic-ack-gate",
            "--evidence",
            "pull_request:owner/repo#42",
            "--verified-by",
            "submitter",
            *self.env_arguments(),
            expect=1,
        )
        self.assertIn("submitter", self_confirmed["error"])

        verified = self.curate(
            "verify",
            "--entry-id",
            "synthetic-ack-gate",
            "--evidence",
            "pull_request:owner/repo#42",
            "--verified-by",
            "reviewer-x",
            *self.env_arguments(),
        )
        self.assertEqual(verified["entry_status"], "verified")

        exported = self.run_script(
            EXPORT,
            "--knowledge-dir",
            str(self.knowledge),
            "--export-dir",
            str(self.export),
            "--origin-repo",
            "owner/fork",
            "--contributor",
            "submitter",
            "--entry",
            "synthetic-ack-gate",
        )
        self.assertEqual(exported["status"], "passed")
        bundle = Path(exported["bundle"]["documents"][0])
        document = v2.load_document(bundle)
        self.assertEqual(document["layer"], v2.EXPORT_LAYER)
        self.assertEqual(document["entries"][0]["slug"], "synthetic-ack-gate")
        self.assertEqual(
            document["entries"][0]["provenance"]["origin_repo"], "owner/fork"
        )
        # Local bookkeeping fields must not leave the fork.
        self.assertNotIn("_kind", document["entries"][0])

        # Idempotency: an unchanged entry is not proposed twice.
        again = self.run_script(
            EXPORT,
            "--knowledge-dir",
            str(self.knowledge),
            "--export-dir",
            str(self.export),
            "--origin-repo",
            "owner/fork",
            "--contributor",
            "submitter",
            "--entry",
            "synthetic-ack-gate",
        )
        self.assertEqual(again["exportable"], [])
        self.assertEqual(
            [item["slug"] for item in again["unchanged"]], ["synthetic-ack-gate"]
        )

        validated = self.run_script(VALIDATE, "--knowledge-dir", str(self.knowledge))
        self.assertEqual(validated["status"], "passed")

    def test_export_of_an_unknown_slug_is_not_reported_as_success(self) -> None:
        payload = self.run_script(
            EXPORT,
            "--knowledge-dir",
            str(self.knowledge),
            "--export-dir",
            str(self.export),
            "--origin-repo",
            "owner/fork",
            "--entry",
            "no-such-entry",
            expect=1,
        )
        self.assertEqual(payload["not_found"], ["no-such-entry"])
        self.assertEqual(payload["exportable"], [])

    def test_duplicate_fingerprint_needs_an_explicit_override(self) -> None:
        first = self.capture(*self.env_arguments())["candidate_id"]
        self.curate(
            "promote",
            "--candidate-id",
            first,
            "--entry-id",
            "synthetic-ack-gate",
            "--origin-repo",
            "owner/fork",
        )
        second = self.capture(*self.env_arguments())["candidate_id"]
        rejected = self.curate(
            "promote",
            "--candidate-id",
            second,
            "--entry-id",
            "synthetic-ack-gate-2",
            "--origin-repo",
            "owner/fork",
            expect=1,
        )
        self.assertIn("synthetic-ack-gate", rejected["error"])
        accepted = self.curate(
            "promote",
            "--candidate-id",
            second,
            "--entry-id",
            "synthetic-ack-gate-2",
            "--origin-repo",
            "owner/fork",
            "--force-new",
        )
        self.assertEqual(accepted["entry_id"], "synthetic-ack-gate-2")

    def test_v2_deprecation_keeps_the_reason_out_of_the_document(self) -> None:
        candidate_id = self.capture(*self.env_arguments())["candidate_id"]
        self.curate(
            "promote",
            "--candidate-id",
            candidate_id,
            "--entry-id",
            "synthetic-ack-gate",
            "--origin-repo",
            "owner/fork",
        )
        deprecated = self.curate(
            "deprecate",
            "--entry-id",
            "synthetic-ack-gate",
            "--reason",
            "Synthetic lifecycle completed.",
        )
        self.assertEqual(deprecated["action"], "deprecated")
        document = v2.load_document(
            self.knowledge / f"known-failure-signatures{v2.V2_SUFFIX}"
        )
        entry = next(
            item
            for item in document["entries"]
            if item["slug"] == "synthetic-ack-gate"
        )
        self.assertEqual(entry["status"], "deprecated")
        # v2 has no field for the reason; it is archived locally instead.
        self.assertNotIn("Synthetic lifecycle completed.", json.dumps(document))
        self.assertIn(
            "Synthetic lifecycle completed.",
            Path(deprecated["reason_path"]).read_text(encoding="utf-8"),
        )


class ThreeLayerQueryTest(V2FlowBase):
    def promote_and_verify(self) -> None:
        candidate_id = self.capture(*self.env_arguments())["candidate_id"]
        self.curate(
            "promote",
            "--candidate-id",
            candidate_id,
            "--entry-id",
            "synthetic-ack-gate",
            "--origin-repo",
            "owner/fork",
            "--contributor",
            "submitter",
        )
        for dimension, arguments in (
            ("topology", ["--values", "tp2"]),
            ("execution_mode", ["--values", "eager"]),
            (
                "model",
                ["--any-basis", "reproduced on two unrelated model families"],
            ),
        ):
            self.curate(
                "resolve",
                "--entry-id",
                "synthetic-ack-gate",
                "--dimension",
                dimension,
                *arguments,
            )
        self.curate(
            "verify",
            "--entry-id",
            "synthetic-ack-gate",
            "--evidence",
            "pull_request:owner/repo#42",
            "--verified-by",
            "reviewer-x",
            *self.env_arguments(),
        )

    def test_envelope_names_layers_and_source_ref(self) -> None:
        from vaws_knowledge.corpus import installed_commit

        payload = self.query("--query", FINGERPRINT)
        self.assertIn("shared", payload["layers_available"])
        self.assertIn("project", payload["layers_available"])
        self.assertEqual(payload["source_repo"], "vllm-ascend-workspace/vaws-knowledge")
        self.assertEqual(payload["source_ref"], installed_commit())
        self.assertEqual(payload["absent_fact_semantics"], "unknown")

    def test_absent_service_is_never_reported_as_supported(self) -> None:
        payload = self.query("--query", "wholly unknown zzzqqq behaviour")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["results"], [])
        self.assertIn("unknown", payload["no_result_meaning"].lower())

    def test_unverified_project_entry_needs_an_explicit_opt_in(self) -> None:
        candidate_id = self.capture(*self.env_arguments())["candidate_id"]
        self.curate(
            "promote",
            "--candidate-id",
            candidate_id,
            "--entry-id",
            "synthetic-ack-gate",
            "--origin-repo",
            "owner/fork",
        )
        default = self.query("--query", FINGERPRINT)
        self.assertNotIn(
            "synthetic-ack-gate", [match["slug"] for match in default["results"]]
        )
        opted_in = self.query("--query", FINGERPRINT, "--include-unverified")
        match = next(
            item for item in opted_in["results"] if item["slug"] == "synthetic-ack-gate"
        )
        self.assertEqual(match["status"], "unverified")
        self.assertTrue(any("unverified" in item.lower() for item in match.get("warnings") or []))

    def test_verified_entry_is_returned_by_default_with_its_layer(self) -> None:
        self.promote_and_verify()
        payload = self.query("--query", FINGERPRINT)
        match = next(
            item for item in payload["results"] if item["slug"] == "synthetic-ack-gate"
        )
        self.assertEqual(match["layer"], "project")
        self.assertEqual(match["body"], "rule")
        self.assertIn("soc", {item["dimension"] for item in match["applicability"]["dimensions"]})

    def test_shared_layer_reads_the_installed_corpus(self) -> None:
        from vaws_knowledge.corpus import installed_commit

        payload = self.query(
            "--query",
            "Ascend910B4",
            "--layer",
            "shared",
            "--include-unverified",
            "--bodies",
            "measurement",
        )
        self.assertEqual({match["layer"] for match in payload["results"]}, {"shared"})
        self.assertEqual(payload["source_repo"], "vllm-ascend-workspace/vaws-knowledge")
        self.assertEqual(payload["source_ref"], installed_commit())
        self.assertEqual(payload["results"][0]["body"], "measurement")
        self.assertFalse(payload["degraded"] and "shared" in payload["layers_absent"])

    def test_candidate_layer_is_available_before_review(self) -> None:
        self.capture(*self.env_arguments())
        payload = self.query(
            "--query", FINGERPRINT, "--layer", "candidate", "--include-unverified"
        )
        self.assertEqual(
            {match["layer"] for match in payload["results"]}, {"candidate"}
        )

    def test_promote_removes_the_candidate_layer_entry(self) -> None:
        captured = self.capture(*self.env_arguments())
        before = self.query(
            "--query", FINGERPRINT, "--layer", "candidate", "--include-unverified"
        )
        self.assertEqual(
            {match["layer"] for match in before["results"]}, {"candidate"}
        )
        self.curate(
            "promote",
            "--candidate-id",
            captured["candidate_id"],
            "--entry-id",
            "synthetic-ack-gate",
            "--origin-repo",
            "owner/fork",
        )
        leftover = self.query(
            "--query", FINGERPRINT, "--layer", "candidate", "--include-unverified"
        )
        self.assertEqual(leftover["results"], [])
        project = self.query("--query", FINGERPRINT, "--include-unverified")
        match = next(
            item for item in project["results"] if item["slug"] == "synthetic-ack-gate"
        )
        self.assertEqual(match["layer"], "project")
        self.assertFalse(
            list(self.commons_candidates.glob("*.yaml"))
            and any(
                captured["candidate_id"] in path.read_text(encoding="utf-8")
                for path in self.commons_candidates.glob("*.yaml")
            )
        )

    def test_v1_entries_stay_queryable_through_v1_reader(self) -> None:
        from vaws_knowledge_v1 import query_knowledge

        matches = query_knowledge(
            knowledge_dir=self.knowledge, query="deepseek v3.1 layers"
        )
        self.assertTrue(matches)

    def test_v2_entries_are_fetchable_by_slug_and_uuid(self) -> None:
        self.promote_and_verify()
        by_slug = self.query("--id", "synthetic-ack-gate")
        self.assertTrue(by_slug["found"])
        self.assertEqual(by_slug["entry"]["slug"], "synthetic-ack-gate")
        by_uuid = self.query("--id", by_slug["entry"]["uuid"])
        self.assertEqual(by_uuid["entry"]["slug"], "synthetic-ack-gate")

    def test_broken_project_document_does_not_break_the_query(self) -> None:
        (self.knowledge / f"model-capabilities{v2.V2_SUFFIX}").write_text(
            '{"schema_version": 2, "kind": "model-capabilities"}\n', encoding="utf-8"
        )
        payload = self.query("--query", FINGERPRINT)
        self.assertIn("results", payload)
        self.assertIn("load", payload)


if __name__ == "__main__":
    unittest.main()
