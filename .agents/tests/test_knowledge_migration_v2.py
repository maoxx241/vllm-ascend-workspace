#!/usr/bin/env python3
"""Tests for the v1 -> v2 knowledge migration.

The fixture reproduces the shape that actually sits in the v1 corpus,
including the internal address range, because the two properties under test
are exactly the ones that string breaks: nothing may be guessed, and the
address must not survive into a migrated document or into the report.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))

import vaws_knowledge_v2 as v2  # noqa: E402
import vaws_knowledge_migrate as migrate  # noqa: E402

MIGRATE = ROOT / ".agents" / "scripts" / "knowledge_migrate_v2.py"

# Same shape as the real corpus entry, with a synthetic RFC 1918 range
# standing in for the leaked one: pinning real infrastructure addresses in a
# tracked test file would be the leak these assertions exist to prevent.
ADDRESS_FRAGMENT = "10.198.51"
V1_APPLICABILITY = (
    "Verified 2026-09-02 on workspace A3 containers (vllm-ascend images), "
    f"TP2/TP4/TP8/TP16 services on {ADDRESS_FRAGMENT}.100-103."
)


def v1_entry(entry_id: str = "gloo-init-hostname-missing") -> dict:
    return {
        "id": entry_id,
        "source": "Observed while bringing up services during session work.",
        "applicable_versions": V1_APPLICABILITY,
        "updated_at": "2026-09-02",
        "status": "active",
        "rule": {
            "summary": "Service start fails because /etc/hosts lacks the hostname",
            "owner_skill": "vllm-ascend-serving",
            "scope": {"component": ["service-bootstrap"]},
            "fingerprints": ["gloo init hostname resolution failed"],
            "symptom": "The engine exits during init with a hostname lookup error.",
            "root_cause": "The container hostname has no /etc/hosts mapping.",
            "resolution": "Map the container hostname to the loopback address.",
            "avoidance": "Check the mapping before starting the service.",
            "confidence": "high",
            "occurrence_count": 3,
        },
    }


def v1_document(entries: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "kind": "known-failure-signatures",
        "updated_at": "2026-09-02",
        "entries": entries,
    }


class DerivationTests(unittest.TestCase):
    """Only mechanically derivable facts may be read out of v1 free text."""

    def test_soc_and_topology_are_derived_from_the_string(self) -> None:
        self.assertEqual(migrate.derive_soc(V1_APPLICABILITY), ["A3"])
        self.assertEqual(
            migrate.derive_topology(V1_APPLICABILITY),
            ["tp2", "tp4", "tp8", "tp16"],
        )

    def test_no_version_is_invented_from_an_image_reference(self) -> None:
        # "vllm-ascend images" names no version; guessing one is the failure
        # mode this whole migration exists to avoid.
        self.assertEqual(migrate.derive_versions(V1_APPLICABILITY), {})

    def test_explicit_versions_are_read_when_actually_present(self) -> None:
        derived = migrate.derive_versions(
            "CANN 8.2.RC1, driver 25.0.rc1.1, torch 2.7.1, torch_npu 2.7.1, "
            "vllm 0.11.0, vllm-ascend 0.11.0rc1"
        )
        self.assertEqual(derived["cann"], "8.2.RC1")
        self.assertEqual(derived["driver"], "25.0.rc1.1")
        self.assertEqual(derived["vllm"], "0.11.0")
        self.assertEqual(derived["vllm_ascend"], "0.11.0rc1")
        self.assertNotEqual(derived["torch_npu"], derived.get("vllm"))


class MigrateEntryTests(unittest.TestCase):
    def migrate_one(self, entry: dict | None = None, **kwargs) -> dict:
        return migrate.migrate_entry(
            entry or v1_entry(),
            kind="known-failure-signatures",
            origin_repo="owner/vllm-ascend-workspace",
            contributor="anonymous",
            **kwargs,
        )

    def test_undecidable_dimensions_become_unresolved_markers(self) -> None:
        report = self.migrate_one()
        entry = report["entry"]
        pending = v2.unresolved_dimensions(entry)
        for dimension in ("cann", "driver", "python_abi", "torch", "torch_npu",
                          "vllm", "vllm_ascend"):
            self.assertIn(dimension, pending)
        self.assertEqual(
            {item["dimension"] for item in report["needs_human_input"]}, set(pending)
        )
        for item in report["needs_human_input"]:
            self.assertTrue(item["needs"].strip())

    def test_derived_dimensions_are_bounded_not_unresolved(self) -> None:
        entry = self.migrate_one()["entry"]
        self.assertEqual(entry["scope"]["soc"], {"values": ["A3"]})
        self.assertEqual(
            entry["scope"]["topology"], {"values": ["tp2", "tp4", "tp8", "tp16"]}
        )
        self.assertEqual(entry["scope"]["component"], {"values": ["service-bootstrap"]})

    def test_no_dimension_is_silently_widened_to_any(self) -> None:
        entry = self.migrate_one()["entry"]
        widened = [
            name
            for name, constraint in entry["scope"].items()
            if constraint.get("any") is True
        ]
        self.assertEqual(widened, [])

    def test_migrated_entry_cannot_be_verified_or_exported(self) -> None:
        entry = self.migrate_one()["entry"]
        self.assertEqual(entry["status"], "unverified")
        self.assertNotEqual(entry["confidence"], "high")
        self.assertEqual(v2.validate_entry(entry, context=v2.PROJECT_LAYER), [])
        with self.assertRaisesRegex(v2.KnowledgeV2Error, "unresolved"):
            v2.export_entry(entry, contributor="handle", origin_repo="owner/fork")

    def test_internal_addresses_reach_neither_entry_nor_report(self) -> None:
        report = self.migrate_one()
        self.assertNotIn(ADDRESS_FRAGMENT, json.dumps(report))
        self.assertTrue(
            any("ip-address" in note for note in report["removed"]),
            report["removed"],
        )

    def test_identity_is_stable_across_repeated_migration(self) -> None:
        first = self.migrate_one()["entry"]
        second = self.migrate_one()["entry"]
        self.assertEqual(first["uuid"], second["uuid"])
        self.assertEqual(first["content_hash"], second["content_hash"])

    def test_existing_uuid_is_preserved(self) -> None:
        existing = v2.new_uuid()
        entry = self.migrate_one(existing_uuid=existing)["entry"]
        self.assertEqual(entry["uuid"], existing)

    def test_lost_v1_metadata_is_reported_not_dropped_silently(self) -> None:
        report = self.migrate_one()
        joined = " ".join(report["notes"])
        self.assertIn("verification", joined)
        self.assertIn("source", joined)

    def test_structured_document_family_is_blocked_not_flattened(self) -> None:
        report = migrate.migrate_entry(
            {
                "id": "glm-layer-count",
                "source": "model card",
                "applicable_versions": "GLM-4.5 only",
                "updated_at": "2026-08-01",
                "status": "active",
                "rule": {"expected_layers": 92, "structure": {"dense": 3}},
            },
            kind="model-capabilities",
            origin_repo="owner/vllm-ascend-workspace",
            contributor="anonymous",
        )
        self.assertNotIn("entry", report)
        self.assertTrue(report["blocked"])


class MigrateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "known-failure-signatures.yaml").write_text(
            json.dumps(v1_document([v1_entry()]), indent=2) + "\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *arguments: str) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(MIGRATE),
                "--knowledge-dir",
                str(self.root),
                "--origin-repo",
                "owner/vllm-ascend-workspace",
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_dry_run_writes_nothing(self) -> None:
        payload = self.run_cli("--dry-run")
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(list(self.root.glob("*.v2.yaml")), [])

    def test_migration_writes_v2_and_keeps_v1_readable(self) -> None:
        before = (self.root / "known-failure-signatures.yaml").read_bytes()
        payload = self.run_cli("--report", str(self.root / "MIGRATION-v2.md"))
        self.assertEqual(payload["status"], "passed")
        v2_path = self.root / f"known-failure-signatures{v2.V2_SUFFIX}"
        self.assertTrue(v2_path.is_file())
        # Dual-read: the v1 document is left byte-identical.
        self.assertEqual(
            (self.root / "known-failure-signatures.yaml").read_bytes(), before
        )
        entries, problems = v2.load_entries(self.root)
        self.assertEqual(problems, [])
        self.assertEqual(len(entries), 1)

    def test_report_lists_required_human_input_without_the_address(self) -> None:
        report_path = self.root / "MIGRATION-v2.md"
        self.run_cli("--report", str(report_path))
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("gloo-init-hostname-missing", report)
        self.assertIn("cann", report)
        self.assertNotIn(ADDRESS_FRAGMENT, report)

    def test_rerun_is_idempotent(self) -> None:
        self.run_cli()
        first = (self.root / f"known-failure-signatures{v2.V2_SUFFIX}").read_bytes()
        self.run_cli()
        self.assertEqual(
            (self.root / f"known-failure-signatures{v2.V2_SUFFIX}").read_bytes(), first
        )


if __name__ == "__main__":
    unittest.main()
