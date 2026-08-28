"""Linux process-level checks for managed start gates and owned cleanup.

No NPU dependencies; run in the remote CPU test environment, not on a Mac.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / ".remote-dev"), str(ROOT / ".agents/lib")]
from core.managed_jobs import control_job, process_identity
from vaws_npu_coordination import process_guard_busy


@unittest.skipUnless(sys.platform == "linux", "requires Linux /proc identity")
class ManagedJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = (ROOT / ".remote-dev/core/managed_jobs.py").read_text()
        self.identifiers = []

    def tearDown(self):
        for identifier in self.identifiers:
            for _ in range(50):
                status = self.call(identifier, "stop", force=True)
                if status["quiet"]:
                    break
                time.sleep(0.02)
        self.temp.cleanup()

    def call(self, identifier, action, **args):
        return control_job({"root": str(self.root), "job_id": identifier, "action": action, **args}, self.source)

    def prepare(self, letter, command, timeout=10):
        identifier = "vaws-" + letter * 64
        self.identifiers.append(identifier)
        status = self.call(identifier, "prepare", spec={"cwd": str(self.root), "command": command, "env": {}, "timeout_seconds": timeout})
        return identifier, status

    def go(self, identifier):
        return self.call(identifier, "go", authorization={"run_id": identifier, "epoch": "test", "fence": 1})

    def until(self, identifier, condition):
        for _ in range(100):
            status = self.call(identifier, "status")
            if condition(status):
                return status
            time.sleep(0.05)
        self.fail("managed job did not reach the expected bounded state: " + json.dumps(status))

    def test_waiting_gate_is_idempotent_and_command_does_not_run_early(self):
        identifier, first = self.prepare("a", "printf completed > result.txt")
        second = self.call(identifier, "prepare", spec={"cwd": str(self.root), "command": "printf completed > result.txt", "env": {}, "timeout_seconds": 10})
        self.assertEqual(first["receipt"]["pid"], second["receipt"]["pid"])
        self.assertEqual(second["state"], "prepared")
        self.assertFalse((self.root / "result.txt").exists())
        self.assertTrue(process_guard_busy(first["receipt"]["process_guard"]))
        self.go(identifier)
        self.go(identifier)
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["state"], "succeeded")
        self.assertEqual((self.root / "result.txt").read_text(), "completed")

    def test_stop_clean_environment_daemon_keeps_the_other_family_alive(self):
        command = ("setsid env -u VAWS_REMOTE_JOB_TOKEN " + shlex.quote(sys.executable)
                   + " -c " + shlex.quote("import os,time; from pathlib import Path; Path('daemon.pid').write_text(str(os.getpid())); time.sleep(60)") + " &")
        a, _ = self.prepare("a", command, timeout=60)
        b, before = self.prepare("b", "sleep 60 & wait", timeout=60)
        self.go(a)
        self.go(b)
        self.until(a, lambda row: (self.root / "daemon.pid").exists())
        daemon = int((self.root / "daemon.pid").read_text())
        observed = self.call(a, "status")
        self.assertIn(daemon, [row["pid"] for row in observed["processes"]])
        self.assertNotIn(b"VAWS_REMOTE_JOB_TOKEN=", Path(f"/proc/{daemon}/environ").read_bytes())
        self.assertNotEqual(process_identity(daemon)["pgid"], observed["receipt"]["pgid"])
        self.call(a, "stop", force=True)
        self.until(a, lambda row: row["quiet"])
        self.assertIsNone(process_identity(daemon))
        after = self.call(b, "status")
        self.assertFalse(after["quiet"])
        self.assertEqual(before["receipt"]["pid"], after["receipt"]["pid"])

    def test_stop_before_go_never_executes_and_unknown_receipt_is_not_free(self):
        identifier, _ = self.prepare("a", "touch should-not-exist")
        self.call(identifier, "stop", force=True)
        self.until(identifier, lambda row: row["quiet"])
        with self.assertRaises(RuntimeError):
            self.go(identifier)
        self.assertFalse((self.root / "should-not-exist").exists())
        directory = self.root / ".vaws-runtime/remote-dev/jobs" / ("vaws-" + "b" * 64)
        directory.mkdir()
        (directory / "intent.json").write_text("{}")
        status = self.call("vaws-" + "b" * 64, "status")
        self.assertEqual(status["state"], "uncertain")
        self.assertFalse(status["quiet"])

    def test_timeout_is_not_reported_as_a_success_or_manual_cancel(self):
        identifier, _ = self.prepare("a", "sleep 60", timeout=1)
        self.go(identifier)
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["state"], "timeout")

    def test_background_descendant_cannot_outlive_the_bounded_execution_unobserved(self):
        identifier, _ = self.prepare("a", "sleep 60 &", timeout=1)
        self.go(identifier)
        observed = self.until(identifier, lambda row: row["quiet"])
        self.assertEqual(observed["state"], "timeout")
        self.assertTrue(observed["result"]["descendants_drained"])

    def test_lost_supervisor_cannot_report_quiet_or_release_its_retained_guard(self):
        identifier, first = self.prepare("a", "touch should-not-run")
        os.kill(first["receipt"]["pid"], signal.SIGKILL)
        observed = self.until(identifier, lambda row: not row.get("processes"))
        self.assertEqual(observed["state"], "uncertain")
        self.assertFalse(observed["quiet"])
        # Model a completely readable, empty host process view. An unprivileged
        # CI runner may not read root-owned environ; that must remain unknown.
        with mock.patch("vaws_npu_coordination.Path.iterdir", return_value=iter([])):
            self.assertTrue(process_guard_busy(first["receipt"]["process_guard"]))
            self.assertFalse(process_guard_busy(first["receipt"]["process_guard"], completion_confirmed=True))
        self.assertFalse((self.root / "should-not-run").exists())


if __name__ == "__main__":
    unittest.main()
