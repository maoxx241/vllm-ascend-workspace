#!/usr/bin/env python3
"""Shared utilities for ascend-memory-profiling scripts."""

from __future__ import annotations

import json
import shlex
import sys
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
from vaws_task_target import execution_target, task_client, task_id_of  # noqa: E402

MEMPROF_STATE_DIR = ROOT / ".vaws-local" / "memory-profiling"

ENV_PREAMBLE = (
    "source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null; "
    "source /usr/local/Ascend/nnal/atb/set_env.sh 2>/dev/null; "
    "export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:"
    "/usr/local/Ascend/driver/lib64/driver:"
    "/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH}; "
)


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
        execution_id = client.resolve_execution(service=service)
        if not execution_id:
            raise RuntimeError("memory profiling needs --execution-id or a live named service")
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
        "devices": (target.get("environment") or {}).get("ASCEND_RT_VISIBLE_DEVICES"),
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
    service: str = "vllm",
    state_repo_root: Path = ROOT,
) -> dict[str, Any] | None:
    """Read the serving skill's persisted receipt for a task."""
    return load_task_serving_state(task_id, service=service, repo_root=state_repo_root)


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


def msprof_wrapper_script(mem_freq: int = 1) -> str:
    if mem_freq <= 0:
        raise ValueError("msprof memory frequency must be positive")
    return f'''#!/bin/bash
# VAWS memory profiler wrapper
set -e
command -v msprof >/dev/null
SERVE_SCRIPT="$1"
RUNTIME_DIR="$2"
MSPROF_OUT="$RUNTIME_DIR/msprof_data"
exec msprof --output="$MSPROF_OUT" \\
  --sys-hardware-mem=on --sys-hardware-mem-freq={mem_freq} \\
  --task-time=off --ai-core=off --ascendcl=off \\
  --application="$(printf 'bash %q' "$SERVE_SCRIPT")"
'''


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
    reports_path = f"{msprof_output_dir}/_reports.json"
    ssh_write_text(ep, _MSPROF_REPORTS_CONFIG, reports_path)
    result = ssh_exec(
        ep,
        f"{ENV_PREAMBLE} msprof --export=on --output={shlex.quote(msprof_output_dir)}"
        f" --reports={shlex.quote(reports_path)} > {shlex.quote(log_file)} 2>&1",
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"msprof export failed (exit {result.returncode}); remote log: {log_file}")

    progress(f"msprof export complete: {len(prof_dirs)} PROF directories")
    return prof_dirs
