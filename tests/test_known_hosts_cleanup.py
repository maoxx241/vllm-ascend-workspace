import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib import reset


def test_run_known_hosts_cleanup_removes_host_and_port_variants(monkeypatch):
    calls = []

    def fake_run(command, check, capture_output, text):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reset.subprocess, "run", fake_run)

    reset._run_known_hosts_cleanup("10.0.0.12", 41001)

    assert calls == [
        ["ssh-keygen", "-R", "10.0.0.12"],
        ["ssh-keygen", "-R", "[10.0.0.12]:41001"],
        ["ssh-keygen", "-R", "10.0.0.12:41001"],
    ]
