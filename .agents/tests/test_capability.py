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

from vaws_capability import (  # noqa: E402
    CAPABILITY_DEPS,
    CAPABILITY_ORDER,
    FLEET_REMEDY,
    build_doctor_envelope,
    dumps_doctor,
    evaluate_capabilities,
)
from vaws_result_envelope import validate_envelope  # noqa: E402


class DoctorEnvelopeTests(unittest.TestCase):
    def test_doctor_envelope_is_partial_when_spec_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = build_doctor_envelope(
                argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
                repo_root=Path(tmp),
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
        self.assertTrue(any("python .agents/scripts/vaws_deps.py sync" in command for command in commands), commands)

    def test_envelope_lint_accepts_doctor_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = build_doctor_envelope(
                argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
                repo_root=Path(tmp),
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
        env = {key: value for key, value in os.environ.items()}
        env["HOME"] = tempfile.mkdtemp()
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws_deps.py"), "doctor"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=str(ROOT),
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema_version"], "vaws.result-compact.v1")
        self.assertIn("record_ref", payload)
        self.assertIn("collecting workspace capability report", proc.stderr)
        self.assertIn(payload["outcome"], {"success", "partial", "failure", "blocked"})

    def test_doctor_cli_full_prints_complete_envelope(self) -> None:
        env = {key: value for key, value in os.environ.items()}
        env["HOME"] = tempfile.mkdtemp()
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws_deps.py"), "doctor", "--full"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=str(ROOT),
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema_version"], "vaws.result-envelope.v1")


class ResolverDegradationTests(unittest.TestCase):
    def test_host_npu_authority_depends_on_coordinator_package(self) -> None:
        self.assertEqual(CAPABILITY_DEPS["host_npu_authority"], ("vaws-coordinator",))
        with tempfile.TemporaryDirectory() as tmp:
            envelope = build_doctor_envelope(
                argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
                repo_root=Path(tmp),
            )
        cap = envelope["extensions"]["capability_report"]["capabilities"]["host_npu_authority"]
        self.assertFalse(cap["available"])
        self.assertTrue(cap["degraded"])
        self.assertEqual(cap["depends_on"], ["vaws-coordinator"])
        self.assertTrue(any("python .agents/scripts/vaws_deps.py sync" in (item.get("remedy") or "") for item in cap["degradation"]))

    def test_capability_deps_use_distribution_names(self) -> None:
        self.assertEqual(CAPABILITY_DEPS["remote_endpoints"], ("vaws-remote-dev",))
        self.assertEqual(CAPABILITY_DEPS["task_pool"], ("vaws-coordinator",))
        self.assertEqual(CAPABILITY_DEPS["shared_knowledge"], ("vaws-knowledge",))
        self.assertEqual(CAPABILITY_DEPS["fleet_observation"], ("uvx", "vaws-top"))

    def test_fleet_observation_without_uvx_uses_deploy_remedy(self) -> None:
        from unittest.mock import patch

        with patch("vaws_capability.shutil.which", return_value=None):
            report = evaluate_capabilities()
        cap = report["capabilities"]["fleet_observation"]
        self.assertFalse(cap["available"])
        self.assertTrue(cap["degraded"])
        self.assertEqual(cap["depends_on"], ["uvx", "vaws-top"])
        self.assertTrue(cap["degradation"])
        self.assertEqual(cap["degradation"][0]["remedy"], FLEET_REMEDY)
        self.assertEqual(
            FLEET_REMEDY,
            "python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py deploy",
        )

    def test_acknowledged_drift_is_empty(self) -> None:
        envelope = build_doctor_envelope(
            argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
        )
        self.assertEqual(
            envelope["extensions"]["capability_report"]["acknowledged_drift"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
