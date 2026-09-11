"""Real Windows bootstrap exit status, Unicode pipes and descendant lifetime."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

LIB = Path(__file__).resolve().parents[1] / "lib"


def alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel.CloseHandle(handle)


@unittest.skipUnless(os.name == "nt", "Windows job object bootstrap")
class WindowsBootstrapTests(unittest.TestCase):
    def command(self, child):
        code = (
            f"import sys,os;sys.path.insert(0,{str(LIB)!r});"
            "from vaws_windows import run_owned;"
            f"raise SystemExit(run_owned({child!r},env=dict(os.environ)))"
        )
        # Use the base executable so killing the launcher tests its job handle,
        # not a venv redirector between the test and that handle.
        return [str(Path(sys.base_prefix) / "python.exe"), "-c", code]

    def test_unicode_bytes_and_nonzero_exit_survive_venv_hop(self):
        child = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());sys.exit(7)"]
        result = subprocess.run(self.command(child), input="中文 🙂\n".encode(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, "中文 🙂\n".encode())

    def test_killing_launcher_terminates_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "进程.json"
            code = (
                "import os,sys,subprocess,time,json;from pathlib import Path;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
                f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]));"
                "time.sleep(120)"
            )
            process = subprocess.Popen(self.command([sys.executable, "-c", code]),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "bootstrap child never became ready")
                pids = json.loads(marker.read_text(encoding="utf-8"))
                self.assertTrue(all(alive(pid) for pid in pids))
                process.kill()
                process.wait(timeout=5)
                deadline = time.monotonic() + 5
                while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(any(alive(pid) for pid in pids), "bootstrap left an orphan process")
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_normal_exit_preserves_explicitly_detached_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "service-pid.json"
            code = (
                "import sys,subprocess,json;from pathlib import Path;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
                "creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP);"
                f"Path({str(marker)!r}).write_text(json.dumps(child.pid))"
            )
            pid = None
            try:
                result = subprocess.run(self.command([sys.executable, "-c", code]),
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                pid = json.loads(marker.read_text(encoding="utf-8"))
                self.assertTrue(alive(pid), "normal bootstrap exit killed a detached service")
            finally:
                if pid is not None and alive(pid):
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, timeout=10, check=True)
