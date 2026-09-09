#!/usr/bin/env python3
"""Tests for workspace knowledge curation."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
SCRIPT = (
    ROOT
    / ".agents"
    / "skills"
    / "curate-workspace-knowledge"
    / "scripts"
    / "knowledge_curate.py"
)
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_knowledge.server.capture import capture  # noqa: E402
from vaws_knowledge_service import commons_entry, service_config  # noqa: E402

NOW = "2026-07-27T12:00:00Z"


def load_module():
    name = "_knowledge_curate_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


curate = load_module()


def write_knowledge(root: Path, entries: dict[str, list[dict]] | None = None) -> None:
    del entries
    root.mkdir(parents=True, exist_ok=True)


def candidate_payload(
    *,
    evidence_kind: str = "commit",
    stable: bool = True,
    verification_status: str = "passed",
) -> dict:
    return {
        "kind": "known-failure-signatures",
        "summary": "Lease visibility must reach child processes",
        "owner_skill": "session-management",
        "scope": {"component": ["session-runtime"], "machine": ["hvv-sz"]},
        "fingerprints": ["child process sees devices outside its lease"],
        "symptom": "A session-leased job sees every NPU.",
        "root_cause": "The leased device list was not exported to child processes.",
        "resolution": "Export ASCEND_RT_VISIBLE_DEVICES from the session lease.",
        "avoidance": "Build child environments from the session snapshot.",
        "applicable_versions": "workspace revisions before 823df4b",
        "verification": {
            "status": verification_status,
            "checks": ["Child process reported only leased devices."],
        },
        "evidence": [
            {
                "kind": evidence_kind,
                "uri": "commit:823df4b",
                "stable": stable,
            }
        ],
        "confidence": "high",
        "source": {
            "session_id": "test-thread",
            "run_ids": ["lease-real"],
            "commits": ["823df4b"],
        },
    }


CONCRETE_ENVIRONMENT = {
    "soc": "Ascend910_93",
    "cann": "8.2.RC1",
    "driver": "25.0.rc1.1",
    "python_abi": "cp311",
    "torch": "2.7.1",
    "torch_npu": "2.7.1.dev20250724",
    "vllm": "0.11.0",
    "vllm_ascend": "0.11.0rc1",
    "component": "session-runtime",
}


class V2CurationTests(unittest.TestCase):
    """The federated v2 half of the same lifecycle."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.knowledge = self.root / "knowledge"
        self.candidates = self.root / "candidate"
        self.reviewed = self.root / "reviewed"
        write_knowledge(self.knowledge)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture(self, payload: dict | None = None, **environment: str) -> str:
        payload = payload or candidate_payload()
        values = {
            **{name: "unknown" for name in curate.COORDINATE_DIMENSIONS},
            **CONCRETE_ENVIRONMENT,
            **environment,
        }
        payload["environment"] = values
        written = capture(
            commons_entry(payload, values),
            kind=str(payload.get("kind") or "known-failure-signatures"),
            config=service_config(
                self.root,
                project_root=self.knowledge,
                candidate_root=self.candidates,
            ),
        )
        return str(written["slug"])

    def v2_entries(self) -> list[dict]:
        path = self.knowledge / f"known-failure-signatures{curate.v2.V2_SUFFIX}"
        return curate.v2.load_document(path)["entries"]

    def promote(self, **overrides) -> dict:
        arguments = {
            "entry_id": "session-lease-child-visibility",
            "gate_status": "experimental",
            "force_new": False,
            "origin_repo": "owner/fork",
            "contributor": "submitter",
            "candidate_dir": self.candidates,
            "reviewed_dir": self.reviewed,
            "knowledge_dir": self.knowledge,
            "now": NOW,
        }
        candidate_id = overrides.pop("candidate_id", None) or self.capture()
        arguments.update(overrides)
        return curate.promote_candidate_v2(candidate_id, **arguments)

    def test_scope_maps_unknown_to_unresolved_never_to_any(self) -> None:
        scope, pending = curate.scope_from_environment(
            {**CONCRETE_ENVIRONMENT, "model": "unknown"}
        )
        self.assertEqual(scope["soc"], {"values": ["Ascend910_93"]})
        self.assertTrue(curate.v2.is_unresolved(scope["model"]))
        self.assertNotIn("any", scope["model"])
        self.assertIn("model", [item["dimension"] for item in pending])

    def test_promotion_lands_unverified_with_named_gaps(self) -> None:
        result = self.promote()
        entry = self.v2_entries()[0]
        self.assertEqual(result["entry_status"], "unverified")
        self.assertFalse(result["exportable"])
        self.assertEqual(entry["slug"], "session-lease-child-visibility")
        self.assertEqual(entry["provenance"]["origin_repo"], "owner/fork")
        self.assertEqual(
            [item["dimension"] for item in result["needs_human_input"]],
            ["model", "topology", "execution_mode"],
        )
        # 'high' candidate confidence cannot survive into an unverified entry.
        self.assertEqual(entry["confidence"], "low")

    def test_unfollowable_evidence_is_dropped_and_reported(self) -> None:
        candidate_id = self.capture(
            {
                **candidate_payload(evidence_kind="local-log"),
                "environment": {
                    **{name: "unknown" for name in curate.COORDINATE_DIMENSIONS},
                    **CONCRETE_ENVIRONMENT,
                    "source": "explicit",
                },
            }
        )
        result = self.promote(candidate_id=candidate_id)
        self.assertEqual(result["dropped_evidence"], [])
        self.assertNotIn("verification", self.v2_entries()[0])

    def test_resolve_updates_the_content_hash(self) -> None:
        self.promote()
        before = self.v2_entries()[0]["content_hash"]
        result = curate.resolve_dimension(
            "session-lease-child-visibility",
            dimension="topology",
            values=["tp2"],
            basis=None,
            minimum=None,
            maximum=None,
            knowledge_dir=self.knowledge,
            now=NOW,
        )
        self.assertTrue(result["was_unresolved"])
        self.assertNotEqual(result["content_hash"], before)
        self.assertEqual(
            self.v2_entries()[0]["scope"]["topology"], {"values": ["tp2"]}
        )

    def test_resolve_requires_exactly_one_constraint_form(self) -> None:
        self.promote()
        with self.assertRaisesRegex(curate.KnowledgeError, "exactly one"):
            curate.resolve_dimension(
                "session-lease-child-visibility",
                dimension="topology",
                values=["tp2"],
                basis="also independent",
                minimum=None,
                maximum=None,
                knowledge_dir=self.knowledge,
                now=NOW,
            )

    def resolve_all(self) -> None:
        for dimension, arguments in (
            ("topology", {"values": ["tp2"]}),
            ("execution_mode", {"values": ["eager"]}),
            ("model", {"basis": "reproduced on two unrelated model families"}),
        ):
            curate.resolve_dimension(
                "session-lease-child-visibility",
                dimension=dimension,
                values=arguments.get("values"),
                basis=arguments.get("basis"),
                minimum=None,
                maximum=None,
                knowledge_dir=self.knowledge,
                now=NOW,
            )

    def verify(self, **overrides) -> dict:
        arguments = {
            "evidence": ["pull_request:owner/repo#7"],
            "verified_by": ["reviewer-x"],
            "environment": dict(CONCRETE_ENVIRONMENT),
            "verified_at": None,
            "knowledge_dir": self.knowledge,
            "now": NOW,
        }
        arguments.update(overrides)
        return curate.verify_entry("session-lease-child-visibility", **arguments)

    def test_verification_is_refused_while_a_dimension_is_unresolved(self) -> None:
        self.promote()
        with self.assertRaisesRegex(curate.KnowledgeError, "unresolved"):
            self.verify()

    def test_verification_requires_a_second_party(self) -> None:
        self.promote()
        self.resolve_all()
        with self.assertRaisesRegex(curate.KnowledgeError, "second party"):
            self.verify(verified_by=["submitter"])

    def test_verification_requires_a_followable_reference(self) -> None:
        self.promote()
        self.resolve_all()
        with self.assertRaisesRegex(curate.KnowledgeError, "evidence must be"):
            self.verify(evidence=["screenshot:/tmp/shot.png"])

    def test_verification_requires_a_concrete_environment(self) -> None:
        self.promote()
        self.resolve_all()
        incomplete = {**CONCRETE_ENVIRONMENT, "cann": "unknown"}
        with self.assertRaisesRegex(curate.KnowledgeError, "cann"):
            self.verify(environment=incomplete)

    def test_verified_entry_is_export_clean(self) -> None:
        self.promote()
        self.resolve_all()
        result = self.verify()
        entry = self.v2_entries()[0]
        self.assertEqual(result["entry_status"], "verified")
        self.assertEqual(entry["verification"]["verified_by"], ["reviewer-x"])
        self.assertEqual(
            curate.v2.validate_entry(entry, context="export"), []
        )

    def test_list_unresolved_reports_what_a_human_must_supply(self) -> None:
        self.promote()
        payload = curate.list_unresolved(self.knowledge)
        blocked = payload["blocked"][0]
        self.assertEqual(blocked["entry_id"], "session-lease-child-visibility")
        self.assertEqual(
            [item["dimension"] for item in blocked["unresolved"]],
            ["model", "topology", "execution_mode"],
        )
        for item in blocked["unresolved"]:
            self.assertTrue(item["needs"])

    def test_deprecation_reason_is_archived_outside_the_document(self) -> None:
        self.promote()
        result = curate.deprecate_entry_v2(
            "session-lease-child-visibility",
            superseded_by=None,
            reason="Superseded by a runtime fix.",
            knowledge_dir=self.knowledge,
            reviewed_dir=self.reviewed,
            now=NOW,
        )
        entry = self.v2_entries()[0]
        self.assertEqual(entry["status"], "deprecated")
        self.assertNotIn("Superseded by a runtime fix.", json.dumps(entry))
        self.assertTrue(Path(result["reason_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
