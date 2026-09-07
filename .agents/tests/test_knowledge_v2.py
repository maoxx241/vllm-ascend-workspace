#!/usr/bin/env python3
"""Tests for the federated knowledge v2 client library.

Covers the three parts a fork cannot get wrong: the canonicalization that
decides whether upstream sees a re-proposal as the same entry, the coordinate
contract that keeps an unresolved dimension from being read as "applies
anywhere", and the export gate.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))

import vaws_knowledge_v2 as v2  # noqa: E402

NOW = "2026-09-07T00:00:00Z"


def bounded_scope() -> dict:
    return {
        "soc": {"values": ["Ascend910_93"]},
        "cann": {"values": ["8.2.RC1"]},
        "driver": {"values": ["25.0.rc1.1"]},
        "python_abi": {"values": ["cp311"]},
        "torch": {"values": ["2.7.1"]},
        "torch_npu": {"values": ["2.7.1.dev20250724"]},
        "vllm": {"values": ["0.11.0"]},
        "vllm_ascend": {"values": ["0.11.0rc1"]},
        "model": {"any": True, "basis": "reproduced with two unrelated model families"},
        "topology": {"values": ["tp2", "tp8"]},
        "execution_mode": {"values": ["eager", "aclgraph"]},
        "component": {"values": ["service-bootstrap"]},
    }


def sample_entry(**overrides) -> dict:
    entry = {
        "uuid": v2.derived_uuid("owner/fork", "known-failure-signatures", "sample"),
        "slug": "sample",
        "content_hash": "sha256:" + "0" * 64,
        "status": "unverified",
        "confidence": "medium",
        "scope": bounded_scope(),
        "provenance": {
            "contributor": "submitter",
            "origin_repo": "owner/fork",
            "submitted_at": "2026-09-07",
            "redaction_profile": "r1",
        },
        "lifecycle": {
            "first_seen": "2026-09-01",
            "updated_at": "2026-09-07",
            "superseded_by": None,
            "resolved_by": None,
        },
        "rule": {
            "summary": "Service bootstrap aborts before the engine starts",
            "symptom": "The launcher exits during init with a bootstrap error.",
            "root_cause": "The bootstrap precondition is not satisfied.",
            "resolution": "Satisfy the precondition before launching.",
            "fingerprints": ["bootstrap precondition not satisfied"],
        },
    }
    entry.update(overrides)
    return v2.with_content_hash(entry)


def verification() -> dict:
    return {
        "evidence": [{"type": "pull_request", "ref": "owner/repo#1"}],
        "verified_by": ["reviewer"],
        "verified_against": {
            "soc": "Ascend910_93",
            "cann": "8.2.RC1",
            "driver": "25.0.rc1.1",
            "torch": "2.7.1",
            "torch_npu": "2.7.1.dev20250724",
            "vllm": "0.11.0",
            "vllm_ascend": "0.11.0rc1",
        },
        "last_verified_at": "2026-09-07",
    }


class CanonicalizationTests(unittest.TestCase):
    """docs/federation.md: the hash decides upstream idempotency."""

    def test_hash_covers_only_scope_and_rule(self) -> None:
        entry = sample_entry()
        restated = deepcopy(entry)
        restated["provenance"]["contributor"] = "someone-else"
        restated["lifecycle"]["updated_at"] = "2026-12-31"
        restated["status"] = "deprecated"
        self.assertEqual(v2.content_hash(entry), v2.content_hash(restated))

    def test_hash_ignores_key_order_and_insignificant_whitespace(self) -> None:
        entry = sample_entry()
        reordered = deepcopy(entry)
        reordered["rule"] = {
            "fingerprints": list(entry["rule"]["fingerprints"]),
            "resolution": entry["rule"]["resolution"] + "\n",
            "root_cause": "\r\n" + entry["rule"]["root_cause"],
            "symptom": entry["rule"]["symptom"] + "  ",
            "summary": entry["rule"]["summary"],
        }
        self.assertEqual(v2.content_hash(entry), v2.content_hash(reordered))

    def test_fingerprints_are_order_and_case_insensitive(self) -> None:
        entry = sample_entry()
        entry["rule"]["fingerprints"] = ["alpha signature", "beta signature"]
        other = deepcopy(entry)
        other["rule"]["fingerprints"] = ["BETA   signature", "Alpha signature"]
        self.assertEqual(v2.content_hash(entry), v2.content_hash(other))

    def test_hash_changes_when_the_claim_changes(self) -> None:
        entry = sample_entry()
        narrowed = deepcopy(entry)
        narrowed["scope"]["topology"] = {"values": ["tp2"]}
        self.assertNotEqual(v2.content_hash(entry), v2.content_hash(narrowed))

    def test_canonical_payload_is_deterministic_json(self) -> None:
        payload = v2.canonical_payload(sample_entry())
        self.assertEqual(set(json.loads(payload)), {"rule", "scope"})
        self.assertEqual(payload, v2.canonical_payload(sample_entry()))

    def test_derived_uuid_is_stable_and_v4_shaped(self) -> None:
        first = v2.derived_uuid("owner/fork", "kind", "slug")
        second = v2.derived_uuid("owner/fork", "kind", "slug")
        self.assertEqual(first, second)
        self.assertRegex(first, v2.UUID_RE)
        self.assertNotEqual(first, v2.derived_uuid("other/fork", "kind", "slug"))


class CoordinateContractTests(unittest.TestCase):
    def test_unresolved_marker_blocks_verified_status(self) -> None:
        entry = sample_entry()
        entry["scope"]["cann"] = v2.unresolved_constraint(
            "the CANN version of the verification container"
        )
        entry["verification"] = verification()
        entry["status"] = "verified"
        entry = v2.with_content_hash(entry)
        errors = v2.validate_entry(entry, context=v2.PROJECT_LAYER)
        self.assertTrue(any("unresolved dimensions" in error for error in errors))

    def test_unresolved_marker_is_allowed_while_unverified(self) -> None:
        entry = sample_entry()
        entry["scope"]["driver"] = v2.unresolved_constraint(
            "the driver version of the verification host"
        )
        entry = v2.with_content_hash(entry)
        self.assertEqual(v2.validate_entry(entry, context=v2.PROJECT_LAYER), [])
        self.assertEqual(v2.unresolved_dimensions(entry), ["driver"])

    def test_unresolved_marker_is_refused_in_export_context(self) -> None:
        entry = sample_entry()
        entry["scope"]["driver"] = v2.unresolved_constraint(
            "the driver version of the verification host"
        )
        entry = v2.with_content_hash(entry)
        errors = v2.validate_entry(entry, context="export")
        self.assertTrue(any("unresolved" in error for error in errors))

    def test_any_requires_a_stated_basis(self) -> None:
        entry = sample_entry()
        entry["scope"]["model"] = {"any": True}
        entry = v2.with_content_hash(entry)
        self.assertTrue(v2.validate_entry(entry, context=v2.PROJECT_LAYER))

    def test_every_dimension_must_be_declared(self) -> None:
        entry = sample_entry()
        del entry["scope"]["component"]
        entry = v2.with_content_hash(entry)
        errors = v2.validate_entry(entry, context=v2.PROJECT_LAYER)
        self.assertTrue(any("component" in error for error in errors))

    def test_empty_values_list_is_not_a_bound(self) -> None:
        entry = sample_entry()
        entry["scope"]["topology"] = {"values": []}
        entry = v2.with_content_hash(entry)
        self.assertTrue(v2.validate_entry(entry, context=v2.PROJECT_LAYER))


class StatusGateTests(unittest.TestCase):
    def test_verified_requires_evidence_and_a_confirming_handle(self) -> None:
        entry = sample_entry(status="verified")
        errors = v2.validate_entry(entry, context=v2.PROJECT_LAYER)
        self.assertTrue(any("verification is required" in error for error in errors))

    def test_verified_with_full_verification_passes(self) -> None:
        entry = sample_entry()
        entry["status"] = "verified"
        entry["verification"] = verification()
        entry = v2.with_content_hash(entry)
        self.assertEqual(v2.validate_entry(entry, context="export"), [])

    def test_high_confidence_needs_a_confirmed_status(self) -> None:
        entry = sample_entry(confidence="high")
        errors = v2.validate_entry(entry, context=v2.PROJECT_LAYER)
        self.assertTrue(any("confidence high" in error for error in errors))

    def test_resolved_entry_must_point_at_its_fix(self) -> None:
        entry = sample_entry()
        entry["status"] = "resolved"
        entry = v2.with_content_hash(entry)
        errors = v2.validate_entry(entry, context=v2.PROJECT_LAYER)
        self.assertTrue(any("resolved_by" in error for error in errors))

    def test_content_hash_mismatch_is_reported(self) -> None:
        entry = sample_entry()
        entry["rule"]["summary"] = "reworded without rehashing"
        errors = v2.validate_entry(entry, context=v2.PROJECT_LAYER)
        self.assertTrue(any("content_hash" in error for error in errors))

    def test_undeclared_fields_are_refused(self) -> None:
        entry = sample_entry()
        entry["internal_note"] = "not part of the egress whitelist"
        errors = v2.validate_entry(entry, context=v2.PROJECT_LAYER)
        self.assertTrue(any("egress whitelist" in error for error in errors))


class DocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, entries: list[dict]) -> Path:
        document = v2.new_document("known-failure-signatures", now=NOW)
        document["entries"] = entries
        path = self.root / f"known-failure-signatures{v2.V2_SUFFIX}"
        v2.write_document(path, document)
        return path

    def test_round_trip_and_load_entries(self) -> None:
        self.write([sample_entry()])
        entries, problems = v2.load_entries(self.root)
        self.assertEqual(problems, [])
        self.assertEqual([entry["slug"] for entry in entries], ["sample"])
        self.assertEqual(entries[0]["_kind"], "known-failure-signatures")

    def test_documents_are_json_compatible_yaml(self) -> None:
        path = self.write([sample_entry()])
        # Parsed by the standard library alone, so a missing PyYAML still reads.
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["schema_version"], 2
        )

    def test_duplicate_slug_is_refused(self) -> None:
        with self.assertRaisesRegex(v2.KnowledgeV2Error, "duplicated"):
            self.write([sample_entry(), sample_entry()])

    def test_write_refuses_a_blocked_value(self) -> None:
        entry = sample_entry()
        entry["rule"]["resolution"] = "ssh into 10.198.51.100 and restart"
        with self.assertRaises(Exception) as caught:
            self.write([v2.with_content_hash(entry)])
        self.assertIn("ip-address", str(caught.exception))

    def test_malformed_document_degrades_to_a_problem(self) -> None:
        path = self.root / f"model-capabilities{v2.V2_SUFFIX}"
        path.write_text('{"schema_version": 2}\n', encoding="utf-8")
        entries, problems = v2.load_entries(self.root)
        self.assertEqual(entries, [])
        self.assertTrue(problems)


class ExportTests(unittest.TestCase):
    def test_export_restamps_provenance_and_rehashes(self) -> None:
        entry = sample_entry()
        entry["status"] = "verified"
        entry["verification"] = verification()
        entry = v2.with_content_hash(entry)
        exported = v2.export_entry(
            entry,
            contributor="handle",
            origin_repo="owner/fork",
            submitted_at="2026-09-07",
        )
        self.assertEqual(exported["provenance"]["contributor"], "handle")
        self.assertEqual(exported["provenance"]["redaction_profile"], "r1")
        self.assertEqual(exported["content_hash"], v2.content_hash(entry))

    def test_export_refuses_unresolved_dimensions(self) -> None:
        entry = sample_entry()
        entry["scope"]["cann"] = v2.unresolved_constraint("the verified CANN version")
        entry = v2.with_content_hash(entry)
        with self.assertRaisesRegex(v2.KnowledgeV2Error, "unresolved"):
            v2.export_entry(entry, contributor="handle", origin_repo="owner/fork")

    def test_export_refuses_export_severity_findings(self) -> None:
        entry = sample_entry()
        entry["status"] = "verified"
        entry["verification"] = verification()
        entry["rule"]["resolution"] = "remount /mnt/weight/GLM-4.5 before launch"
        entry = v2.with_content_hash(entry)
        with self.assertRaises(Exception) as caught:
            v2.export_entry(entry, contributor="handle", origin_repo="owner/fork")
        self.assertIn("internal-mount-path", str(caught.exception))

    def test_exported_document_declares_the_unverified_layer(self) -> None:
        entry = sample_entry()
        entry["status"] = "verified"
        entry["verification"] = verification()
        document = v2.export_document(
            "known-failure-signatures",
            [v2.with_content_hash(entry)],
            contributor="handle",
            origin_repo="owner/fork",
            submitted_at="2026-09-07",
        )
        self.assertEqual(document["layer"], v2.EXPORT_LAYER)
        self.assertNotEqual(document["layer"], v2.PROJECT_LAYER)


class MatchViewTests(unittest.TestCase):
    def test_scope_summary_states_unknown_dimensions_explicitly(self) -> None:
        entry = sample_entry()
        entry["scope"]["cann"] = v2.unresolved_constraint("the verified CANN version")
        summary = v2.scope_summary(entry["scope"])
        self.assertIn("cann", summary)
        self.assertIn("unresolved", summary.lower())

    def test_default_result_set_excludes_unverified(self) -> None:
        self.assertFalse(v2.is_default_result(sample_entry()))
        verified = sample_entry()
        verified["status"] = "verified"
        self.assertTrue(v2.is_default_result(verified))

    def test_stale_entries_carry_a_warning(self) -> None:
        stale = sample_entry()
        stale["status"] = "stale"
        self.assertIn("stale", (v2.status_warning(stale) or "").lower())
        self.assertIn("unverified", (v2.status_warning(sample_entry()) or "").lower())
        verified = sample_entry()
        verified["status"] = "verified"
        verified["verification"] = verification()
        self.assertIsNone(v2.status_warning(verified))


if __name__ == "__main__":
    unittest.main()
