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

from vaws_knowledge import KNOWLEDGE_FILES, capture_candidate  # noqa: E402

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


class CurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.knowledge = self.root / "knowledge"
        self.candidates = self.root / "candidates"
        self.reviewed = self.root / "reviewed"
        write_knowledge(self.knowledge)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture(self, payload: dict | None = None) -> str:
        result = capture_candidate(
            payload or candidate_payload(),
            candidate_dir=self.candidates,
            knowledge_dir=self.knowledge,
            now=NOW,
        )
        return result["candidate_id"]

    def formal_entries(self) -> list[dict]:
        payload = json.loads(
            (self.knowledge / "known-failure-signatures.yaml").read_text(
                encoding="utf-8"
            )
        )
        return payload["entries"]

    def test_list_is_compact(self) -> None:
        candidate_id = self.capture()
        listed = curate.list_candidates(self.candidates)
        self.assertEqual(listed[0]["candidate_id"], candidate_id)
        self.assertNotIn("root_cause", listed[0])

    def test_experimental_promotion_writes_formal_and_archives(self) -> None:
        candidate_id = self.capture()
        result = curate.promote_candidate(
            candidate_id,
            entry_id="session-lease-child-visibility",
            status="experimental",
            force_new=False,
            candidate_dir=self.candidates,
            reviewed_dir=self.reviewed,
            knowledge_dir=self.knowledge,
            now=NOW,
        )
        entry = self.formal_entries()[0]
        self.assertEqual(result["action"], "promoted")
        self.assertEqual(entry["id"], "session-lease-child-visibility")
        self.assertEqual(entry["rule"]["candidate_id"], candidate_id)
        self.assertFalse((self.candidates / f"{candidate_id}.json").exists())
        self.assertTrue((self.reviewed / f"{candidate_id}.json").is_file())

    def test_inconclusive_candidate_cannot_be_promoted(self) -> None:
        candidate_id = self.capture(
            candidate_payload(verification_status="inconclusive")
        )
        with self.assertRaisesRegex(curate.KnowledgeError, "inconclusive"):
            curate.promote_candidate(
                candidate_id,
                entry_id=None,
                status="experimental",
                force_new=False,
                candidate_dir=self.candidates,
                reviewed_dir=self.reviewed,
                knowledge_dir=self.knowledge,
                now=NOW,
            )

    def test_unstable_only_candidate_cannot_be_promoted(self) -> None:
        candidate_id = self.capture(candidate_payload(stable=False))
        with self.assertRaisesRegex(curate.KnowledgeError, "stable evidence"):
            curate.promote_candidate(
                candidate_id,
                entry_id=None,
                status="experimental",
                force_new=False,
                candidate_dir=self.candidates,
                reviewed_dir=self.reviewed,
                knowledge_dir=self.knowledge,
                now=NOW,
            )

    def test_active_requires_repeat_or_regression_test(self) -> None:
        candidate_id = self.capture()
        with self.assertRaisesRegex(curate.KnowledgeError, "two occurrences"):
            curate.promote_candidate(
                candidate_id,
                entry_id=None,
                status="active",
                force_new=False,
                candidate_dir=self.candidates,
                reviewed_dir=self.reviewed,
                knowledge_dir=self.knowledge,
                now=NOW,
            )

    def test_active_accepts_stable_regression_test(self) -> None:
        candidate_id = self.capture(
            candidate_payload(evidence_kind="regression-test")
        )
        result = curate.promote_candidate(
            candidate_id,
            entry_id=None,
            status="active",
            force_new=False,
            candidate_dir=self.candidates,
            reviewed_dir=self.reviewed,
            knowledge_dir=self.knowledge,
            now=NOW,
        )
        self.assertEqual(result["entry_status"], "active")

    def test_duplicate_fingerprint_requires_merge_or_force(self) -> None:
        candidate_id = self.capture()
        duplicate = {
            "id": "existing-lease-rule",
            "source": "existing evidence",
            "applicable_versions": "all",
            "updated_at": "2026-07-27",
            "status": "active",
            "rule": {
                "summary": "Existing",
                "fingerprints": ["child process sees devices outside its lease"],
            },
        }
        write_knowledge(
            self.knowledge, {"known-failure-signatures": [duplicate]}
        )
        with self.assertRaisesRegex(curate.KnowledgeError, "use merge"):
            curate.promote_candidate(
                candidate_id,
                entry_id="new-lease-rule",
                status="experimental",
                force_new=False,
                candidate_dir=self.candidates,
                reviewed_dir=self.reviewed,
                knowledge_dir=self.knowledge,
                now=NOW,
            )

    def test_merge_updates_existing_evidence_and_occurrences(self) -> None:
        candidate_id = self.capture()
        existing = {
            "id": "existing-lease-rule",
            "source": "existing evidence",
            "applicable_versions": "old",
            "updated_at": "2026-07-26",
            "status": "experimental",
            "rule": {
                "candidate_id": "older-candidate",
                "candidate_ids": ["older-candidate"],
                "summary": "Old summary",
                "owner_skill": "session-management",
                "scope": {},
                "fingerprints": ["older fingerprint"],
                "symptom": "Old",
                "root_cause": "Old",
                "resolution": "Old",
                "avoidance": "",
                "verification": {"status": "passed", "checks": ["old"]},
                "evidence": [
                    {"kind": "commit", "uri": "commit:old", "stable": True}
                ],
                "confidence": "medium",
                "occurrence_count": 1,
                "first_seen_at": "2026-07-26T12:00:00Z",
                "last_verified_at": "2026-07-26T12:00:00Z",
            },
        }
        write_knowledge(
            self.knowledge, {"known-failure-signatures": [existing]}
        )
        result = curate.merge_candidate(
            candidate_id,
            entry_id="existing-lease-rule",
            candidate_dir=self.candidates,
            reviewed_dir=self.reviewed,
            knowledge_dir=self.knowledge,
            now=NOW,
        )
        entry = self.formal_entries()[0]
        self.assertEqual(result["action"], "merged")
        self.assertEqual(entry["rule"]["occurrence_count"], 2)
        self.assertIn(candidate_id, entry["rule"]["candidate_ids"])
        self.assertEqual(len(entry["rule"]["evidence"]), 2)

    def test_reject_does_not_modify_formal_knowledge(self) -> None:
        candidate_id = self.capture()
        before = (self.knowledge / "known-failure-signatures.yaml").read_text(
            encoding="utf-8"
        )
        result = curate.reject_candidate(
            candidate_id,
            reason="Infrastructure-only transient.",
            candidate_dir=self.candidates,
            reviewed_dir=self.reviewed,
            now=NOW,
        )
        after = (self.knowledge / "known-failure-signatures.yaml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(result["action"], "rejected")
        self.assertEqual(before, after)

    def test_deprecate_retains_entry_and_replacement(self) -> None:
        old = {
            "id": "old-rule",
            "source": "old evidence",
            "applicable_versions": "old",
            "updated_at": "2026-07-26",
            "status": "active",
            "rule": {},
        }
        new = {
            "id": "new-rule",
            "source": "new evidence",
            "applicable_versions": "new",
            "updated_at": "2026-07-27",
            "status": "active",
            "rule": {},
        }
        write_knowledge(
            self.knowledge, {"known-failure-signatures": [old, new]}
        )
        curate.deprecate_entry(
            "old-rule",
            superseded_by="new-rule",
            reason="New transport supersedes it.",
            knowledge_dir=self.knowledge,
            now=NOW,
        )
        entries = {entry["id"]: entry for entry in self.formal_entries()}
        self.assertEqual(entries["old-rule"]["status"], "deprecated")
        self.assertEqual(
            entries["old-rule"]["rule"]["deprecation"]["superseded_by"],
            "new-rule",
        )


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
        self.candidates = self.root / "candidates"
        self.reviewed = self.root / "reviewed"
        write_knowledge(self.knowledge)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture(self, payload: dict | None = None, **environment: str) -> str:
        payload = payload or candidate_payload()
        payload["environment"] = {
            **{name: "unknown" for name in curate.COORDINATE_DIMENSIONS},
            **CONCRETE_ENVIRONMENT,
            **environment,
            "source": "explicit",
        }
        result = capture_candidate(
            payload,
            candidate_dir=self.candidates,
            knowledge_dir=self.knowledge,
            now=NOW,
        )
        return result["candidate_id"]

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
        self.assertEqual(entry["confidence"], "medium")

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
        self.assertTrue(result["dropped_evidence"])
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
