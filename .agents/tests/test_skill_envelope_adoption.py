#!/usr/bin/env python3
"""Load-bearing remaining CLIs emit Result Envelope v1 on stdout.

These commands are hermetic: they take the local-miss path and never start a
remote workload.
"""

from __future__ import annotations

import json
import os
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
    import jsonschema  # noqa: PLC0415

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(envelope, schema)


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "VAWS_CONTEXT_FILE"}
    return subprocess.run(
        [sys.executable, str(script), "status", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        env=env,
    )


def _lint(stdout: str) -> dict:
    return envelope_lint.check_payload(stdout)


class LoadBearingSkillEnvelopeTests(unittest.TestCase):
    def test_serve_status_missing_context_emits_envelope(self) -> None:
        script = ROOT / ".agents/skills/vllm-ascend-serving/scripts/serving.py"
        completed = _run(script)
        report = _lint(completed.stdout)
        self.assertTrue(report["valid"], report["findings"])
        payload = json.loads(completed.stdout)
        validate_envelope(payload)
        _validate_tracked_schema(payload)
        self.assertEqual(payload["operation"]["skill"], "vllm-ascend-serving")
        self.assertEqual(payload["outcome"], "failure")

if __name__ == "__main__":
    unittest.main()
