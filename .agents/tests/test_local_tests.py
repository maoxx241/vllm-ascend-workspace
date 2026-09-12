"""Real pytest children exercise receipts, failures, retries and process ownership."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from contextlib import redirect_stdout
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
    # Linux may expose a zombie before reaping it, or remove its proc entry
    # between kill(0) and read_text(). Neither state is a surviving child.
    if sys.platform == "linux":
        try:
            status = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return status.rsplit(") ", 1)[1][0] != "Z"
    return True


class ProcessObservationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux proc observation")
    def test_reaped_between_signal_probe_and_proc_read_is_dead(self):
        for error in (FileNotFoundError, ProcessLookupError):
            with self.subTest(error=error.__name__):
                with patch.object(os, "kill"), patch.object(Path, "read_text", side_effect=error):
                    self.assertFalse(alive(12345))


class LocalTestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".vaws-local").mkdir()

    def write(self, name, body):
        (self.root / name).write_text(body, encoding="utf-8")

    def execute(self, cases, **kwargs):
        progress = io.StringIO()
        receipt, code = runner.run(self.root, cases, progress=progress, heartbeat=0.2, **kwargs)
        return receipt, code, progress.getvalue()

    def assert_dead(self, pid):
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(pid), f"owned process {pid} survived")

    def test_failure_continues_other_files_and_explicit_rerun_only_reports_selected_cases(self):
        self.write("test_pass.py", "def test_pass():\n    assert True\n")
        self.write("test_flaky.py", "from pathlib import Path\ndef test_flaky():\n    assert Path('.vaws-local/fixed').exists()\n")
        # A normal run needs neither a Git repository nor a Git executable.
        with patch.dict(os.environ, {"PATH": ""}):
            first, code, progress = self.execute(["test_flaky.py", "test_pass.py"], jobs=2)
        self.assertEqual(code, 1)
        self.assertEqual([row["pytest_exit_code"] for row in first["cases"]], [1, 0])
        self.assertEqual([row["status"] for row in first["cases"]], ["failed", "passed"])
        self.assertIn("[start] test_pass.py", progress)
        self.assertEqual(json.loads(Path(first["summary"]).read_text()), first)
        (self.root / ".vaws-local/fixed").touch()
        output = io.StringIO()
        with redirect_stdout(output):
            code = runner.main(self.root, ["--rerun-failed", first["summary"]])
        second = json.loads(output.getvalue())
        self.assertEqual(code, 0, second)
        self.assertEqual([(row["case"], row["status"]) for row in second["cases"]], [("test_flaky.py", "passed")])
        self.assertNotEqual(second["summary"], first["summary"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = runner.main(self.root, ["--rerun-failed", second["summary"]])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "nothing_to_rerun")

    def test_rerun_resolves_checkout_alias_without_accepting_outside_paths(self):
        physical = self.root / "physical checkout"
        physical.mkdir()
        (physical / ".vaws-local").mkdir()
        (physical / "test_flaky.py").write_text(
            "from pathlib import Path\ndef test_flaky():\n    assert Path('.vaws-local/fixed').exists()\n",
            encoding="utf-8")
        first, code = runner.run(physical, ["test_flaky.py"], progress=io.StringIO())
        self.assertEqual(code, 1)
        alias = self.root / "checkout alias"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(physical), str(alias))
            self.addCleanup(os.rmdir, alias)
        else:
            alias.symlink_to(physical, target_is_directory=True)
        (physical / ".vaws-local/fixed").touch()
        output = io.StringIO()
        with redirect_stdout(output):
            code = runner.main(alias, ["--rerun-failed", first["summary"]])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual([(row["case"], row["status"]) for row in result["cases"]],
                         [("test_flaky.py", "passed")])
        (self.root / "outside.py").write_text("def test_outside(): pass\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inside the repository"):
            runner.select_cases(alias, ["../outside.py"], "file")

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


if __name__ == "__main__":
    unittest.main()
