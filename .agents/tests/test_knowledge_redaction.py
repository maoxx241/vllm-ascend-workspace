#!/usr/bin/env python3
"""Tests for the source-side redaction ruleset (profile r1).

The fork is where redaction has to happen: a public PR cannot be recalled.
These tests pin both directions — what must be refused, and what must keep
working, because a rule that flags every public repo name would make the
export gate unusable and get switched off.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))

import vaws_redaction as redaction  # noqa: E402

# RFC 1918, deliberately not a range this project uses.
SYNTHETIC_ADDRESS = "10.198.51.100"


def rules_for(value: object) -> set[str]:
    return {finding.rule for finding in redaction.scan(value, path="payload")}


class BlockSeverityTests(unittest.TestCase):
    """Findings that must never reach a tracked file."""

    def test_private_address_range_is_blocked(self) -> None:
        # Same shape as the value that leaked into the v1 corpus. The vector is
        # synthetic on purpose: a test that pins real infrastructure addresses
        # in a tracked file is the leak it is meant to prevent.
        text = f"TP2/TP4/TP8/TP16 services on {SYNTHETIC_ADDRESS}-{200}."
        self.assertIn("ip-address", rules_for(text))

    def test_public_and_loopback_addresses_stay_usable(self) -> None:
        self.assertEqual(rules_for("bind 127.0.0.1:8000 and ::1 only"), set())

    def test_hostname_user_path_and_mail_are_blocked(self) -> None:
        self.assertIn("hostname", rules_for("node-a7.corp-internal.invalid timed out"))
        self.assertIn("user-home-path", rules_for("/home/testuser/vllm-ascend"))
        self.assertIn("email-address", rules_for("owner is dev@corp-mail.invalid"))

    def test_secret_shaped_keys_and_values_are_blocked(self) -> None:
        self.assertIn("secret-key-name", rules_for({"api_token": "x"}))
        self.assertIn(
            "secret-value", rules_for("HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz012345")
        )

    def test_module_paths_and_versions_are_not_hostnames(self) -> None:
        # A rule that flags torch.distributed or 8.2.RC1 would make every
        # knowledge entry unwritable.
        self.assertEqual(
            rules_for("torch.distributed init failed on CANN 8.2.RC1, vllm 0.11.0"),
            set(),
        )

    def test_require_writable_refuses_block_findings(self) -> None:
        with self.assertRaisesRegex(redaction.RedactionError, "ip-address"):
            redaction.require_writable({"note": f"reachable at {SYNTHETIC_ADDRESS}"})

    def test_masking_does_not_reproduce_the_secret(self) -> None:
        findings = redaction.scan("token is hf_abcdefghijklmnopqrstuvwxyz012345")
        masked = [finding.masked for finding in findings]
        self.assertTrue(masked)
        for value in masked:
            self.assertNotIn("abcdefghijklmnop", value)


class ExportSeverityTests(unittest.TestCase):
    """Facts that are legal locally and must not be published."""

    def test_internal_mount_and_container_instance_are_export_only(self) -> None:
        findings = redaction.scan("/mnt/weight/GLM-4.5 mounted in vaws-sess-3f9a")
        severities = {finding.rule: finding.severity for finding in findings}
        self.assertEqual(severities.get("internal-mount-path"), redaction.EXPORT)
        self.assertEqual(severities.get("container-name"), redaction.EXPORT)
        # Export severity must not block a project-layer write.
        redaction.require_writable("/mnt/weight/GLM-4.5 in vaws-sess-3f9a")

    def test_public_project_names_are_not_container_names(self) -> None:
        self.assertEqual(
            rules_for(
                "vllm-ascend-serving started via vaws-knowledge; see "
                "github.com/vllm-ascend-workspace/vaws-knowledge"
            ),
            set(),
        )

    def test_ticket_identifier_is_export_only(self) -> None:
        findings = redaction.scan("tracked as DTS2026091234")
        self.assertEqual(
            {finding.severity for finding in findings}, {redaction.EXPORT}
        )

    def test_require_exportable_refuses_export_findings(self) -> None:
        with self.assertRaisesRegex(redaction.RedactionError, "internal-mount-path"):
            redaction.require_exportable({"note": "weights in /mnt/weight/GLM-4.5"})

    def test_ruleset_is_self_describing(self) -> None:
        rules = redaction.ruleset()
        self.assertTrue(rules)
        self.assertEqual(
            {rule["severity"] for rule in rules}, {redaction.BLOCK, redaction.EXPORT}
        )


class NestedPayloadTests(unittest.TestCase):
    def test_findings_report_the_path_that_carries_the_value(self) -> None:
        payload = {"entries": [{"rule": {"resolution": f"ssh root@{SYNTHETIC_ADDRESS}"}}]}
        findings = redaction.scan(payload, path="doc")
        paths = {finding.path for finding in findings}
        self.assertIn("doc.entries[0].rule.resolution", paths)

    def test_keys_are_screened_as_well_as_values(self) -> None:
        findings = redaction.scan({"/home/testuser/notes": "fine"}, path="doc")
        self.assertIn("user-home-path", {finding.rule for finding in findings})


if __name__ == "__main__":
    unittest.main()
