#!/usr/bin/env python3
"""Workspace capability report and the first Result Envelope v1 producer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_capability import CAPABILITY_ORDER, build_doctor_envelope, dumps_doctor  # noqa: E402
from vaws_dependency import all_pins  # noqa: E402
from vaws_result_envelope import validate_envelope  # noqa: E402


ABSENT_ENV = {
    "VAWS_REMOTE_DEV_ROOT": "/nonexistent-remote-dev",
    "VAWS_COORDINATOR_ROOT": "/nonexistent-vaws-coordinator",
    "VAWS_TOP_ROOT": "/nonexistent-fleet-dashboard",
    "VAWS_KNOWLEDGE_KIT_ROOT": "/nonexistent-vaws-knowledge",
}


def _absent_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("VAWS_")}
    env.update(ABSENT_ENV)
    return env


class DoctorEnvelopeTests(unittest.TestCase):
    def test_doctor_envelope_is_partial_when_deps_are_missing(self) -> None:
        envelope = build_doctor_envelope(
            argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
            env=ABSENT_ENV,
        )
        validate_envelope(envelope)
        self.assertEqual(envelope["outcome"], "partial")
        self.assertEqual(envelope["exit_code"], 1)
        report = envelope["extensions"]["capability_report"]
        self.assertEqual(set(report["capabilities"]), set(CAPABILITY_ORDER))
        self.assertTrue(report["degraded"])
        for entry in report["degradation"]:
            self.assertTrue(str(entry.get("remedy") or "").strip(), entry)
        commands = [item.get("command") or "" for item in envelope["next_step"]["actions"]]
        for pin in all_pins().values():
            self.assertTrue(
                any(pin["bootstrap"] in command for command in commands),
                pin["bootstrap"],
            )

    def test_envelope_lint_accepts_doctor_stdout(self) -> None:
        envelope = build_doctor_envelope(
            argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
            env=ABSENT_ENV,
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            handle.write(dumps_doctor(envelope))
            payload_path = handle.name
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "envelope_lint.py"),
                    "check",
                    "--payload-file",
                    payload_path,
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(ROOT),
            )
        finally:
            Path(payload_path).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lint = json.loads(proc.stdout)
        report = (lint.get("extensions") or {}).get("report") or {}
        self.assertTrue(report.get("valid") or lint.get("valid"), lint)

    def test_doctor_cli_prints_one_json_object(self) -> None:
        env = _absent_env()
        env["HOME"] = tempfile.mkdtemp()
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws_deps.py"), "doctor"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["outcome"], "partial")
        self.assertEqual(payload["schema_version"], "vaws.result-envelope.v1")
        self.assertIn("collecting workspace capability report", proc.stderr)


class ResolverDegradationTests(unittest.TestCase):
    def test_missing_remote_dev_is_dependency_layer(self) -> None:
        envelope = build_doctor_envelope(
            argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
            env=ABSENT_ENV,
        )
        cap = envelope["extensions"]["capability_report"]["capabilities"]["resolver_registration"]
        self.assertTrue(cap["degraded"])
        layers = {item["layer"] for item in cap["degradation"]}
        self.assertIn("dependency", layers)
        self.assertNotIn("client_config", layers)

    def test_acknowledged_drift_is_recorded(self) -> None:
        env = {**ABSENT_ENV, "VAWS_DEPS_ALLOW_OFF_PIN": "vaws-coordinator"}
        envelope = build_doctor_envelope(
            argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
            env=env,
        )
        self.assertEqual(
            envelope["extensions"]["capability_report"]["acknowledged_drift"],
            ["vaws-coordinator"],
        )


if __name__ == "__main__":
    unittest.main()
