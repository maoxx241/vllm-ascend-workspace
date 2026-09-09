#!/usr/bin/env python3
"""P17: the four load-bearing skills emit Result Envelope v1 on stdout.

These commands are hermetic: they take the plan/dry-run or local-miss path
and never start a remote workload.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
for candidate in (LIB, SCRIPTS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from vaws_result_envelope import SCHEMA_VERSION, validate_envelope  # noqa: E402

import envelope_lint  # noqa: E402

SCHEMA_PATH = ROOT / ".agents" / "schemas" / "result-envelope-v1.schema.json"


def _validate_tracked_schema(envelope: dict) -> None:
    """Fail if jsonschema is missing; do not skip."""
    import jsonschema  # noqa: PLC0415

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(envelope, schema)


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )


def _lint(stdout: str) -> dict:
    return envelope_lint.check_payload(stdout)


class LoadBearingSkillEnvelopeTests(unittest.TestCase):
    def test_session_list_emits_envelope(self) -> None:
        script = ROOT / ".agents/skills/session-management/scripts/session_list.py"
        completed = _run(script)
        report = _lint(completed.stdout)
        self.assertTrue(report["valid"], report["findings"])
        payload = json.loads(completed.stdout)
        validate_envelope(payload)
        _validate_tracked_schema(payload)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["operation"]["skill"], "session-management")
        self.assertNotIn("__VAWS_", completed.stdout)

    def test_machine_verify_unmanaged_emits_envelope(self) -> None:
        script = ROOT / ".agents/skills/machine-management/scripts/machine_verify.py"
        completed = _run(script, "--machine", "__g4_no_such_machine__")
        report = _lint(completed.stdout)
        self.assertTrue(report["valid"], report["findings"])
        payload = json.loads(completed.stdout)
        validate_envelope(payload)
        _validate_tracked_schema(payload)
        self.assertEqual(payload["operation"]["skill"], "machine-management")
        self.assertEqual(payload["extensions"]["result"]["status"], "unmanaged")

    def test_serve_status_missing_session_emits_envelope(self) -> None:
        script = ROOT / ".agents/skills/vllm-ascend-serving/scripts/serve_status.py"
        completed = _run(script, "--session-id", "__g4_no_such_session__")
        report = _lint(completed.stdout)
        self.assertTrue(report["valid"], report["findings"])
        payload = json.loads(completed.stdout)
        validate_envelope(payload)
        _validate_tracked_schema(payload)
        self.assertEqual(payload["operation"]["skill"], "vllm-ascend-serving")
        self.assertEqual(payload["outcome"], "failure")

    def test_parity_sync_missing_session_emits_envelope(self) -> None:
        script = ROOT / ".agents/skills/remote-code-parity/scripts/parity_sync.py"
        completed = _run(
            script,
            "--session-id",
            "__g4_no_such_session__",
            "--print-derived-args",
        )
        report = _lint(completed.stdout)
        self.assertTrue(report["valid"], report["findings"])
        payload = json.loads(completed.stdout)
        validate_envelope(payload)
        _validate_tracked_schema(payload)
        self.assertEqual(payload["operation"]["skill"], "remote-code-parity")
        self.assertIn(payload["outcome"], {"failure", "blocked"})


if __name__ == "__main__":
    unittest.main()
