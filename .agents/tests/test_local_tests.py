"""Real pytest children exercise receipts, failures, retries and process ownership."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import vaws_local_tests as runner


def alive(pid):
    if os.name == "nt":
        from vaws_windows import pid_alive
        return pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An already-reaped command can leave a briefly visible zombie on Linux CI.
    status = Path(f"/proc/{pid}/stat")
    return not status.exists() or status.read_text().split(") ", 1)[1][0] != "Z"


class LocalTestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".vaws-local/\n__pycache__/\n.pytest_cache/\n")
        (self.root / ".vaws-local").mkdir()

    def write(self, name, body):
        (self.root / name).write_text(body, encoding="utf-8")

    def execute(self, cases, **kwargs):
        # Process behavior is real; avoid repeatedly hashing the test host's
        # entire dependency installation in this bounded integration fixture.
        progress = io.StringIO()
        with patch.object(runner, "fingerprint", return_value={"digest": runner.source_digest(self.root)}):
            receipt, code = runner.run(self.root, cases, progress=progress, heartbeat=0.2, **kwargs)
        return receipt, code, progress.getvalue()

    def assert_dead(self, pid):
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(pid), f"owned process {pid} survived")

    def test_failure_does_not_stop_other_files_and_unchanged_pass_is_reused(self):
        self.write("test_pass.py", "def test_pass():\n    assert True\n")
        self.write("test_flaky.py", "from pathlib import Path\ndef test_flaky():\n    assert Path('.vaws-local/fixed').exists()\n")
        cases = ["test_flaky.py", "test_pass.py"]
        first, code, progress = self.execute(cases, jobs=2)
        self.assertEqual(code, 1)
        self.assertEqual([row["pytest_exit_code"] for row in first["cases"]], [1, 0])
        self.assertEqual([row["status"] for row in first["cases"]], ["failed", "passed"])
        self.assertIn("[start] test_pass.py", progress)
        self.assertEqual(json.loads(Path(first["summary"]).read_text()), first)
        (self.root / ".vaws-local/fixed").touch()
        second, code, _ = self.execute(cases, previous=first)
        self.assertEqual(code, 0)
        self.assertEqual([row["reused"] for row in second["cases"]], [False, True])
        Path(second["cases"][1]["log"]).write_text("corrupt")
        third, code, _ = self.execute(cases, previous=second)
        self.assertEqual(code, 0)
        self.assertFalse(third["cases"][1]["reused"])
        self.write("helper.py", "value = 2\n")
        fourth, code, _ = self.execute(cases, previous=third)
        self.assertEqual(code, 0)
        self.assertFalse(any(row["reused"] for row in fourth["cases"]))

    def test_crashed_pytest_child_drains_grandchild_and_preserves_exit(self):
        self.write("test_crash.py", """import os, subprocess, sys
from pathlib import Path
def test_crash():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    Path('.vaws-local/pid').write_text(str(child.pid))
    print('before crash', flush=True)
    os._exit(73)
""")
        receipt, code, _ = self.execute(["test_crash.py"])
        row = receipt["cases"][0]
        self.assertEqual((code, row["status"], row["pytest_exit_code"]), (1, "failed", 73))
        self.assertTrue(Path(row["log"]).exists())
        self.assert_dead(int((self.root / ".vaws-local/pid").read_text()))

    def test_timeout_retains_log_emits_heartbeat_and_runs_next_file(self):
        self.write("test_hang.py", """import subprocess, sys, time
from pathlib import Path
def test_hang():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    Path('.vaws-local/pid').write_text(str(child.pid))
    print('still working', flush=True)
    time.sleep(120)
""")
        self.write("test_pass.py", "def test_pass():\n    assert True\n")
        receipt, code, progress = self.execute(["test_hang.py", "test_pass.py"], timeout=4, pytest_args=["-s"])
        self.assertEqual([row["status"] for row in receipt["cases"]], ["timed_out", "passed"])
        self.assertEqual(code, 1)
        self.assertIn("[running] test_hang.py", progress)
        self.assertIn("still working", Path(receipt["cases"][0]["log"]).read_text())
        self.assert_dead(int((self.root / ".vaws-local/pid").read_text()))

    def test_success_exit_without_junit_is_an_error(self):
        self.write("test_false_success.py", "import os\ndef test_exit():\n    os._exit(0)\n")
        receipt, code, _ = self.execute(["test_false_success.py"])
        self.assertEqual((code, receipt["cases"][0]["status"]), (1, "error"))
        self.assertEqual(receipt["cases"][0]["pytest_exit_code"], 0)

    def test_interrupt_retains_partial_receipt_and_drains_children(self):
        self.write("test_interrupt.py", """import subprocess, sys, time
from pathlib import Path
def test_interrupt():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    Path('.vaws-local/pid').write_text(str(child.pid))
    time.sleep(120)
""")
        # Inject SIGINT through Python's real handler in the owning runner.
        # Windows console signals cannot target CREATE_NO_WINDOW children.
        code = f"""import sys, signal, threading, time
from pathlib import Path
sys.path.insert(0, {str(LIB)!r})
import vaws_local_tests as runner
runner.fingerprint = lambda *args, **kwargs: {{'digest': 'fixture'}}
root = Path({str(self.root)!r})
def interrupt():
    deadline = time.monotonic() + 15
    while not (root / '.vaws-local/pid').exists() and time.monotonic() < deadline:
        time.sleep(.05)
    signal.raise_signal(signal.SIGINT)
threading.Thread(target=interrupt, daemon=True).start()
result, status = runner.run(root, ['test_interrupt.py'], heartbeat=.2)
print(result['summary'], flush=True)
raise SystemExit(status)
"""
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=25)
        self.assertEqual(completed.returncode, 130, completed.stderr)
        receipt = json.loads(Path(completed.stdout.strip()).read_text())
        self.assertEqual(receipt["status"], "interrupted")
        self.assertEqual(receipt["cases"][0]["status"], "interrupted")
        self.assertTrue(Path(receipt["cases"][0]["log"]).exists())
        self.assert_dead(int((self.root / ".vaws-local/pid").read_text()))

    def test_fingerprint_tracks_dependency_files_arguments_environment_and_source(self):
        self.write("dependency.py", "x = 1\n")
        class Distribution:
            metadata = {"Name": "fixture"}
            version = "1"
            files = ["dependency.py"]
            def locate_file(_, path):
                return self.root / path
            def read_text(_, path):
                return None
        with patch.object(runner.importlib.metadata, "distributions", return_value=[Distribution()]):
            first = runner.fingerprint(self.root, ["test_a.py"], [])
            self.write("dependency.py", "x = 2\n")
            second = runner.fingerprint(self.root, ["test_a.py"], [])
            self.assertNotEqual(first["parts"]["dependencies"], second["parts"]["dependencies"])
            self.assertNotEqual(second, runner.fingerprint(self.root, ["test_a.py"], ["-x"]))
            with patch.dict(os.environ, {"TEST_RUNNER_INPUT": "changed"}):
                self.assertNotEqual(second, runner.fingerprint(self.root, ["test_a.py"], []))


if __name__ == "__main__":
    unittest.main()
