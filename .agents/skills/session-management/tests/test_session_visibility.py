#!/usr/bin/env python3
"""Regression tests for leased NPU visibility checks."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
MACHINE_SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"
SESSION_SCRIPT = (
    ROOT / ".agents" / "skills" / "session-management" / "scripts" / "session_create.py"
)
for path in (LIB, MACHINE_SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_module():
    spec = importlib.util.spec_from_file_location("_session_visibility_test", SESSION_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


session_create = load_module()


class SessionVisibilityTests(unittest.TestCase):
    def verify(self, observed: str):
        ready = {
            "ok": True,
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
        }
        visibility = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=observed,
            stderr="",
        )
        with (
            mock.patch.object(
                session_create.machine_ops,
                "find_public_key",
                return_value=Path("/keys/id.pub"),
            ),
            mock.patch.object(
                session_create.machine_ops,
                "private_key_for_public_key",
                return_value=Path("/keys/id"),
            ),
            mock.patch.object(
                session_create.machine_ops,
                "check_direct_ssh",
                return_value=ready,
            ),
            mock.patch.object(
                session_create.machine_ops,
                "run_local",
                return_value=visibility,
            ),
        ):
            return session_create.verify_session_ssh(
                {
                    "host": {
                        "ip": "192.0.2.1",
                        "user": "root",
                        "port": 22,
                    }
                },
                container_ssh_port=46001,
                public_key_file=None,
                visible_devices=[0, 1],
            )

    def test_matching_visibility_is_ready(self) -> None:
        result = self.verify("0,1")

        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["device_visibility"]["matches"])

    def test_visibility_drift_needs_repair(self) -> None:
        result = self.verify("")

        self.assertEqual(result["status"], "needs_repair")
        self.assertFalse(result["device_visibility"]["matches"])


if __name__ == "__main__":
    unittest.main()
