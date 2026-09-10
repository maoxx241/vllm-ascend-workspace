#!/usr/bin/env python3
"""Shared utilities for ascend-memory-profiling scripts."""

from __future__ import annotations

import json
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"

for _p in (str(LIB_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vaws_local_state import allocate_run_dir  # noqa: E402
from vaws_remote_dev import ssh_exec, ssh_run_bytes  # noqa: E402
from vaws_result_envelope import progress as envelope_progress  # noqa: E402
from vaws_remote_target import SshEndpoint, ssh_endpoint_from_mapping  # noqa: E402
from vaws_session_state import load_serving_state as load_task_serving_state  # noqa: E402
from vaws_task_target import executions_for_service, execution_target, task_client, task_id_of  # noqa: E402

MEMPROF_STATE_DIR = ROOT / ".vaws-local" / "memory-profiling"

ENV_PREAMBLE = (
    "source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null; "
    "source /usr/local/Ascend/nnal/atb/set_env.sh 2>/dev/null; "
    "export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:"
    "/usr/local/Ascend/driver/lib64/driver:"
    "/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH}; "
)


def ssh_upload(endpoint: SshEndpoint, local_path: Path, remote_path: str) -> None:
    """Upload a file to the remote machine via stdin redirect."""
    with open(local_path, "rb") as f:
        result = ssh_run_bytes(endpoint, f"cat > {shlex.quote(remote_path)}", stdin=f.read())
    if result.returncode != 0:
        raise RuntimeError(f"ssh_upload failed (rc={result.returncode}): {result.stderr!r}")


def ssh_write_text(endpoint: SshEndpoint, content: str, remote_path: str) -> None:
    """Write text content to a remote file via stdin (avoids shell quoting issues)."""
    result = ssh_run_bytes(endpoint, f"cat > {shlex.quote(remote_path)}", stdin=content.encode())
    if result.returncode != 0:
        raise RuntimeError(f"ssh_write_text failed (rc={result.returncode}): {result.stderr!r}")


def progress(msg: str, **extra: Any) -> None:
    envelope_progress("memprof", msg, **extra)


def resolve_execution_target(
    *,
    context_file: str | None = None,
    execution_id: str | None = None,
    service: str = "vllm",
) -> dict[str, Any]:
    """Authoritative coordinator routing for a live or historical execution."""
    client = task_client(context_file)
    task_id = task_id_of(client)
    if not execution_id:
        rows = executions_for_service(client, service)
        if not rows:
            raise RuntimeError("memory profiling needs --execution-id or a live named service")
        execution_id = str(rows[-1].get("id") or rows[-1].get("execution_id"))
    target = execution_target(client, str(execution_id))
    endpoint = ssh_endpoint_from_mapping(target.get("endpoint"))
    return {
        "mode": "execution",
        "record": {"alias": target.get("container_name") or endpoint.host},
        "alias": str(target.get("container_name") or endpoint.host),
        "endpoint": endpoint,
        "task_id": task_id,
        "execution_id": str(execution_id),
        "session_id": task_id,
        "python": target.get("python"),
        "service_port": target.get("service_port"),
        "live": bool(target.get("live")),
        "target": target,
        "client": client,
    }


def ensure_run_dir(tag: str = "") -> Path:
    return allocate_run_dir(MEMPROF_STATE_DIR, tag)


def selected_python(target: dict[str, Any]) -> str:
    """Use the coordinator-selected interpreter. Do not scan fallbacks."""
    python = target.get("python") if isinstance(target, dict) else None
    if not python:
        raise RuntimeError(
            "coordinator target has no python; the selected environment owns the interpreter"
        )
    return str(python)


def load_serving_state(
    task_id: str,
    *,
    state_repo_root: Path = ROOT,
) -> dict[str, Any] | None:
    """Read the serving skill's persisted receipt for a task."""
    del state_repo_root
    return load_task_serving_state(task_id)


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


def _safe_tmp_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return token.strip("._-") or "run"


def upload_msprof_wrapper(ep: SshEndpoint, mem_freq: int = 1, token: str | None = None) -> str:
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
    suffix = _safe_tmp_token(token or f"{os.getpid()}_{uuid.uuid4().hex[:8]}")
    wrapper_path = f"/tmp/_vaws_msprof_wrap_{suffix}.sh"
    ssh_write_text(ep, script, wrapper_path)
    ssh_exec(ep, f"chmod +x {wrapper_path}")
    ssh_write_text(ep, _MSPROF_REPORTS_CONFIG, MSPROF_REPORTS_REMOTE_PATH)
    return wrapper_path


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
