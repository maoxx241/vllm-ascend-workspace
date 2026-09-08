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
from vaws_dependency import all_pins, load_pin  # noqa: E402
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

    def test_host_npu_authority_is_unavailable_without_coordinator(self) -> None:
        envelope = build_doctor_envelope(
            argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
            env=ABSENT_ENV,
        )
        cap = envelope["extensions"]["capability_report"]["capabilities"]["host_npu_authority"]
        self.assertFalse(cap["available"])
        self.assertTrue(cap["degraded"])
        self.assertEqual(cap["depends_on"], ["vaws-coordinator"])
        self.assertTrue(any("bootstrap" in (item.get("remedy") or "") for item in cap["degradation"]))

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


def _write_required(root: Path, pin: dict) -> None:
    for relative in pin["identity"]["required_files"]:
        dest = root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("ok\n", encoding="utf-8")


def _git(root: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)


def _off_pin_coordinator(tmp: str, service_api: dict) -> tuple[Path, dict[str, str]]:
    pin = load_pin("vaws-coordinator")
    root = Path(tmp) / "coord"
    root.mkdir()
    _write_required(root, pin)
    (root / "service-api.json").write_text(json.dumps(service_api), encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "dev")
    _git(root, "remote", "add", "origin", pin["url"])
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "off-pin")
    env = {
        **ABSENT_ENV,
        "VAWS_COORDINATOR_ROOT": str(root),
    }
    return root, env


class DriftedDegradationEffectTests(unittest.TestCase):
    def test_off_pin_compatible_keeps_available_and_names_unpinned_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, env = _off_pin_coordinator(
                tmp,
                {"schema_version": 1, "name": "vaws-coordinator", "service_api_version": 1, "supports": [1]},
            )
            envelope = build_doctor_envelope(
                argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
                env=env,
            )
            report = envelope["extensions"]["capability_report"]
            self.assertEqual(report["deps"]["vaws-coordinator"]["state"], "off_pin")
            self.assertEqual(report["deps"]["vaws-coordinator"]["service_api"]["state"], "compatible")
            for name in ("task_pool", "host_npu_authority"):
                cap = report["capabilities"][name]
                self.assertTrue(cap["available"], name)
                self.assertTrue(cap["degraded"], name)
                self.assertTrue(
                    any(
                        str(item.get("effect") or "").startswith("runs an unpinned build")
                        for item in cap["degradation"]
                    ),
                    name,
                )

    def test_off_pin_incompatible_is_unavailable_with_incompatible_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, env = _off_pin_coordinator(
                tmp,
                {"schema_version": 1, "name": "vaws-coordinator", "service_api_version": 2, "supports": [2]},
            )
            envelope = build_doctor_envelope(
                argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
                env=env,
            )
            report = envelope["extensions"]["capability_report"]
            self.assertEqual(report["deps"]["vaws-coordinator"]["service_api"]["state"], "incompatible")
            for name in ("task_pool", "host_npu_authority"):
                cap = report["capabilities"][name]
                self.assertFalse(cap["available"], name)
                self.assertTrue(cap["degraded"], name)
                self.assertTrue(
                    any(
                        item.get("effect")
                        == "dependent capabilities are degraded/unavailable; execution is not blocked"
                        for item in cap["degradation"]
                    ),
                    name,
                )


if __name__ == "__main__":
    unittest.main()
