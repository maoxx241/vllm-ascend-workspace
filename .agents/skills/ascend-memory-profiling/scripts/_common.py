#!/usr/bin/env python3
"""Shared utilities for ascend-memory-profiling scripts."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
MM_SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"

for _p in (str(LIB_DIR), str(MM_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import inventory as inventory_store  # noqa: E402
from vaws_local_state import ensure_state_dir  # noqa: E402

MEMPROF_STATE_DIR = ROOT / ".vaws-local" / "memory-profiling"
SERVING_STATE_DIR = ROOT / ".vaws-local" / "serving"
PROGRESS_SENTINEL = "__VAWS_MEMPROF_PROGRESS__="

ENV_PREAMBLE = (
    "source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null; "
    "source /usr/local/Ascend/nnal/atb/set_env.sh 2>/dev/null; "
    "export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:"
    "/usr/local/Ascend/driver/lib64/driver:"
    "/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH}; "
)


@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    user: str = "root"

    def destination(self) -> str:
        return f"{self.user}@{self.host}"


def _ssh_base_cmd(endpoint: SshEndpoint) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        "-p", str(endpoint.port),
        endpoint.destination(),
    ]


def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [*_ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(script)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"remote command failed (rc={result.returncode}):\n"
            f"stderr: {result.stderr[:2000]}"
        )
    return result


def ssh_upload(endpoint: SshEndpoint, local_path: Path, remote_path: str) -> None:
    """Upload a file to the remote machine via stdin redirect."""
    cmd = [*_ssh_base_cmd(endpoint), f"cat > {shlex.quote(remote_path)}"]
    with open(local_path, "rb") as f:
        subprocess.run(cmd, stdin=f, check=True, capture_output=True)


def ssh_write_text(endpoint: SshEndpoint, content: str, remote_path: str) -> None:
    """Write text content to a remote file via stdin (avoids shell quoting issues)."""
    cmd = [*_ssh_base_cmd(endpoint), f"cat > {shlex.quote(remote_path)}"]
    subprocess.run(cmd, input=content.encode(), check=True, capture_output=True)


def ssh_bg_exec(
    endpoint: SshEndpoint,
    script: str,
) -> subprocess.Popen:
    cmd = [*_ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(script)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def progress(msg: str, **extra: Any) -> None:
    payload = {"msg": msg, **extra}
    print(f"{PROGRESS_SENTINEL}{json.dumps(payload, ensure_ascii=False)}", file=sys.stderr, flush=True)


def resolve_machine(identifier: str) -> dict[str, Any]:
    read_path = inventory_store.read_inventory_path(
        inventory_store.preferred_inventory_path(inventory_store.DEFAULT_PATH)
    )
    inv = inventory_store.load_inventory(read_path)
    for m in inv.get("machines", []):
        alias = m.get("alias", "")
        host_ip = m.get("host", {}).get("ip", "") if isinstance(m.get("host"), dict) else m.get("host", "")
        if alias == identifier or host_ip == identifier:
            return m
    raise ValueError(f"Machine '{identifier}' not found in inventory")


def endpoint_from_machine(machine: dict[str, Any]) -> SshEndpoint:
    host_info = machine.get("host", {})
    container_info = machine.get("container", {})

    if isinstance(host_info, dict):
        ip = host_info.get("ip", "")
        host_port = host_info.get("port", 22)
        user = host_info.get("user", "root")
    else:
        ip = host_info
        host_port = 22
        user = "root"

    ssh_port = container_info.get("ssh_port", host_port)

    return SshEndpoint(host=ip, port=ssh_port, user=user)


def ensure_run_dir(tag: str = "") -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{tag}" if tag else ts
    d = MEMPROF_STATE_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_python(endpoint: SshEndpoint) -> str:
    """Detect the Python binary with torch_npu on the remote machine."""
    for candidate in [
        "/usr/local/python3.11.14/bin/python3",
        "/usr/local/python3.10/bin/python3",
        "python3",
    ]:
        r = ssh_exec(endpoint, f"{ENV_PREAMBLE} {candidate} -c 'import torch_npu' 2>/dev/null && echo OK", check=False)
        if "OK" in r.stdout:
            return candidate
    raise RuntimeError("No Python with torch_npu found on remote machine")


def load_serving_state(machine_alias: str) -> dict[str, Any] | None:
    """Read the serving skill's persisted state for a given machine.

    Returns None if no state file exists or it's unparseable.
    The state dict contains: model, pid, port, runtime_dir, log_stdout,
    log_stderr, tp, dp, devices, status, started_at, etc.
    """
    path = SERVING_STATE_DIR / f"{machine_alias}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def get_machine_alias(machine: dict[str, Any]) -> str:
    """Extract the alias from a machine inventory entry."""
    return machine.get("alias", machine.get("host", {}).get("ip", "unknown"))


# ---------------------------------------------------------------------------
# msprof environment check
# ---------------------------------------------------------------------------

def check_msprof_available(ep: SshEndpoint) -> dict[str, Any]:
    """Verify that msprof is available on the remote machine.

    Returns {"available": True, "path": str, "version": str} on success.
    Raises RuntimeError with actionable fix instructions on failure.
    """
    r = ssh_exec(
        ep,
        f"{ENV_PREAMBLE} which msprof 2>/dev/null && msprof --version 2>&1 | head -3",
        check=False,
    )
    lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    if r.returncode != 0 or not lines:
        raise RuntimeError(
            "msprof 在远端不可用。显存 profiling 依赖 msprof 采集组件级内存数据。\n"
            "请确认:\n"
            "  1. CANN (ascend-toolkit) 已正确安装\n"
            "  2. /usr/local/Ascend/ascend-toolkit/set_env.sh 可正常 source\n"
            "  3. msprof 在 PATH 中 (通常位于 ascend-toolkit/bin/)\n"
            f"远端输出: stdout={r.stdout[:300]!r}  stderr={r.stderr[:300]!r}"
        )
    msprof_path = lines[0]
    version_info = " ".join(lines[1:]) if len(lines) > 1 else "unknown"
    progress(f"msprof available: {msprof_path} ({version_info})")
    return {"available": True, "path": msprof_path, "version": version_info}


# ---------------------------------------------------------------------------
# msprof wrapping helpers
# ---------------------------------------------------------------------------

MSPROF_WRAPPER_REMOTE_PATH = "/tmp/_vaws_msprof_wrap.sh"


MSPROF_REPORTS_REMOTE_PATH = "/tmp/_vaws_msprof_reports.json"

_MSPROF_REPORTS_CONFIG = json.dumps({
    "json_process": {
        "ascend": False, "acc_pmu": False, "cann": False, "ddr": False,
        "stars_chip_trans": False, "hbm": True, "communication": False,
        "hccs": False, "os_runtime_api": False, "network_usage": False,
        "disk_usage": False, "memory_usage": False, "cpu_usage": False,
        "msproftx": False, "npu_mem": True, "overlap_analyse": False,
        "pcie": False, "sio": False, "stars_soc": False,
        "step_trace": False, "freq": False, "llc": False,
        "nic": False, "roce": False, "qos": False, "device_tx": False,
    }
}, indent=2)


def upload_msprof_wrapper(ep: SshEndpoint, mem_freq: int = 1) -> str:
    """Generate and upload an msprof wrapper script to the remote machine.

    The wrapper receives two arguments from the serving skill's --wrap-script:
      $1 = path to _serve.sh (the vLLM launch script)
      $2 = runtime_dir

    Calls check_msprof_available() first to fail fast if msprof is missing.
    Returns the remote path to the uploaded wrapper.
    """
    check_msprof_available(ep)
    script = f"""\
#!/bin/bash
# msprof wrapper — generated by ascend-memory-profiling skill.
# $1 = serve script path, $2 = runtime dir
SERVE_SCRIPT="$1"
RUNTIME_DIR="$2"
MSPROF_OUT="$RUNTIME_DIR/msprof_data"
exec msprof --output="$MSPROF_OUT" \\
  --sys-hardware-mem=on --sys-hardware-mem-freq={mem_freq} \\
  --task-time=off \\
  --ai-core=off \\
  --ascendcl=off \\
  --application="bash $SERVE_SCRIPT"
"""
    ssh_write_text(ep, script, MSPROF_WRAPPER_REMOTE_PATH)
    ssh_exec(ep, f"chmod +x {MSPROF_WRAPPER_REMOTE_PATH}")
    ssh_write_text(ep, _MSPROF_REPORTS_CONFIG, MSPROF_REPORTS_REMOTE_PATH)
    return MSPROF_WRAPPER_REMOTE_PATH


def run_msprof_export(
    ep: SshEndpoint,
    msprof_output_dir: str,
    timeout: int = 1800,
) -> list[str]:
    """Run msprof --export on all PROF directories under *msprof_output_dir*.

    A single ``msprof --export=on --output=<dir>`` call exports **every**
    PROF_* subdirectory inside *dir*, so we only invoke it once regardless
    of how many PROF directories exist.
    """
    progress("Running msprof export...")
    r = ssh_exec(ep, f"find {shlex.quote(msprof_output_dir)} -maxdepth 1 -name 'PROF_*' -type d 2>/dev/null", check=False)
    prof_dirs = [d.strip() for d in r.stdout.strip().splitlines() if d.strip()]
    if not prof_dirs:
        progress("WARNING: No PROF directories found for msprof export")
        return []

    progress(f"Exporting {len(prof_dirs)} PROF directories (timeout={timeout}s)...")
    log_file = f"{msprof_output_dir}/_export.log"
    reports_arg = ""
    r_chk = ssh_exec(ep, f"test -f {MSPROF_REPORTS_REMOTE_PATH} && echo YES || echo NO", check=False)
    if "YES" in r_chk.stdout:
        reports_arg = f" --reports={MSPROF_REPORTS_REMOTE_PATH}"
    ssh_exec(
        ep,
        f"{ENV_PREAMBLE} msprof --export=on --output={shlex.quote(msprof_output_dir)}"
        f"{reports_arg} > {log_file} 2>&1 &",
        check=False,
        timeout=30,
    )
    import time as _time
    deadline = _time.monotonic() + timeout
    poll_interval = 10
    while _time.monotonic() < deadline:
        _time.sleep(poll_interval)
        r = ssh_exec(ep, "pgrep -f '[m]sprof.*--export' >/dev/null 2>&1 && echo RUNNING || echo DONE", check=False, timeout=15)
        if "DONE" in r.stdout:
            break
        poll_interval = min(poll_interval * 1.5, 60)
    else:
        progress("WARNING: msprof export timed out, proceeding with available CSVs")

    progress(f"msprof export complete: {len(prof_dirs)} PROF directories")
    return prof_dirs
