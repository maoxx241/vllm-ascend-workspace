"""Small OS process snapshots for the workspace's two local launchers.

A persisted PID alone is not ownership: compare birth time and command before
reusing or stopping a process. Unreadable or legacy records remain unowned.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def process_identity(pid: int) -> dict[str, str] | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
                 f"$taskProcess=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
                 "if ($taskProcess -and $taskProcess.CommandLine) { "
                 "@{started=$taskProcess.CreationDate.ToUniversalTime().Ticks.ToString(); "
                 "command=$taskProcess.CommandLine} | ConvertTo-Json -Compress }"],
                capture_output=True, text=True, encoding="utf-8", timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            value = json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else None
            return value if isinstance(value, dict) and value.get("started") and value.get("command") else None
        if sys.platform == "darwin":
            result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "lstart=", "-o", "command="],
                                    capture_output=True, text=True, timeout=10, check=False,
                                    env={**os.environ, "LC_ALL": "C"})
            fields = result.stdout.strip().split(None, 6)
            if result.returncode != 0 or len(fields) != 7 or fields[0].startswith("Z"):
                return None
            return {"started": " ".join(fields[1:6]), "command": fields[6]}
        proc = Path("/proc") / str(pid)
        stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        again = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if not command or again[0] == "Z" or again[19] != stat[19]:
            return None
        return {"started": f"{boot}:{stat[19]}", "command": command}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def same_process(pid: int, identity: Any) -> bool:
    return (isinstance(identity, dict) and bool(identity.get("started"))
            and bool(identity.get("command")) and process_identity(pid) == identity)
