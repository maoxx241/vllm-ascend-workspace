#!/usr/bin/env python3
"""Shared utilities for vllm-ascend-serving scripts."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
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

SERVING_STATE_DIR = ROOT / ".vaws-local" / "serving"
PROGRESS_SENTINEL = "__VAWS_SERVING_PROGRESS__="


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

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
) -> subprocess.CompletedProcess[str]:
    cmd = [*_ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(script)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"remote command failed (rc={result.returncode}):\n"
            f"stderr: {result.stderr[:2000]}"
        )
    return result


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def resolve_machine(identifier: str) -> dict[str, Any]:
    read_path = inventory_store.read_inventory_path(
        inventory_store.preferred_inventory_path(inventory_store.DEFAULT_PATH)
    )
    inv = inventory_store.load_inventory(read_path)
    matches = inventory_store._find_matches(inv, identifier=identifier)
    if not matches:
        raise RuntimeError(f"machine {identifier!r} not found in inventory")
    if len(matches) > 1:
        raise RuntimeError(f"machine {identifier!r} matched multiple records; use a unique alias")
    return matches[0]


def container_endpoint(record: dict[str, Any]) -> SshEndpoint:
    return SshEndpoint(
        host=record["host"]["ip"],
        port=record["container"]["ssh_port"],
    )


def host_endpoint(record: dict[str, Any]) -> SshEndpoint:
    """SSH endpoint for the bare-metal host (not the container).

    Host-level npu-smi can see processes from ALL containers, which is
    essential for reliable NPU occupancy detection.
    """
    return SshEndpoint(
        host=record["host"]["ip"],
        port=record["host"].get("ssh_port", 22),
        user=record["host"].get("user", "root"),
    )


# ---------------------------------------------------------------------------
# Local serving state
# ---------------------------------------------------------------------------

def load_serving_state(machine_alias: str) -> dict[str, Any] | None:
    path = SERVING_STATE_DIR / f"{machine_alias}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_serving_state(machine_alias: str, data: dict[str, Any]) -> Path:
    ensure_state_dir(SERVING_STATE_DIR)
    path = SERVING_STATE_DIR / f"{machine_alias}.json"
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
    return path


# ---------------------------------------------------------------------------
# NPU probe
# ---------------------------------------------------------------------------

_HBM_BUSY_THRESHOLD_MB = 4096


def _parse_npu_smi(output: str) -> dict[str, Any]:
    """Parse ``npu-smi info`` output into a structured dict.

    Returns dict with keys: devices, total, busy, free, free_count, hbm,
    hbm_busy_threshold_mb.
    """
    import re

    dev_ids: set[int] = set()
    hbm: dict[int, dict[str, int]] = {}
    current_npu: int | None = None
    lines = output.splitlines()

    for line in lines:
        hdr = re.match(r"\|\s*(\d+)\s+\d*\w+\d+\w*\s+\|", line)
        if hdr:
            current_npu = int(hdr.group(1))
            dev_ids.add(current_npu)
            continue
        if current_npu is not None and "0000:" in line:
            pairs = re.findall(r"(\d+)\s*/\s*(\d+)", line)
            if len(pairs) >= 2:
                hbm[current_npu] = {
                    "used_mb": int(pairs[-1][0]),
                    "total_mb": int(pairs[-1][1]),
                }
            current_npu = None

    proc_busy: dict[int, list[dict[str, Any]]] = {}
    in_proc = False
    for line in lines:
        if "Process name" in line or "Process memory" in line:
            in_proc = True
            continue
        if in_proc and "No running processes" in line:
            continue
        if in_proc and line.startswith("|"):
            m = re.match(r"\|\s*(\d+)\s+\S+\s+(\d+)\s+(\S+)\s+(\S+)", line)
            if m:
                dev = int(m.group(1))
                proc_busy.setdefault(dev, []).append({
                    "pid": int(m.group(2)),
                    "owner": m.group(3),
                    "name": m.group(4),
                })

    busy: dict[int, list[dict[str, Any]]] = {}
    for dev in sorted(dev_ids):
        reasons: list[dict[str, Any]] = []
        if dev in proc_busy:
            reasons.extend(proc_busy[dev])
        h = hbm.get(dev)
        if h and h["used_mb"] >= _HBM_BUSY_THRESHOLD_MB and dev not in proc_busy:
            reasons.append({
                "pid": None,
                "name": "unknown (HBM occupied, likely another container)",
                "hbm_used_mb": h["used_mb"],
                "detection": "hbm_threshold",
            })
        if reasons:
            busy[dev] = reasons

    free = sorted(d for d in dev_ids if d not in busy)
    return {
        "devices": sorted(dev_ids),
        "total": len(dev_ids),
        "busy": {str(k): v for k, v in sorted(busy.items())},
        "hbm": {str(k): v for k, v in sorted(hbm.items())},
        "free": free,
        "free_count": len(free),
        "hbm_busy_threshold_mb": _HBM_BUSY_THRESHOLD_MB,
    }


def probe_npus(host_ep: SshEndpoint) -> dict[str, Any]:
    """Probe NPU device availability via the **host** (bare-metal) SSH.

    Running npu-smi on the host (not inside a container) is critical because
    the host kernel can see processes from ALL containers.  Inside a single
    container, PID-namespace isolation hides other containers' workloads,
    making process-based occupancy detection unreliable.

    As a secondary signal, HBM usage above ``_HBM_BUSY_THRESHOLD_MB`` marks a
    device as busy even when no visible PID is found (covers edge cases where
    npu-smi does not list the process).
    """
    result = ssh_exec(host_ep, "npu-smi info", check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"npu-smi on host failed (rc={result.returncode}): "
            f"{result.stderr[:500]}"
        )
    parsed = _parse_npu_smi(result.stdout)
    parsed["npu_smi_ok"] = True
    return parsed


def select_devices(
    npu_info: dict[str, Any],
    *,
    requested_devices: str | None,
    tp: int | None,
    dp: int | None = None,
) -> tuple[str | None, str | None]:
    """Validate or auto-select NPU devices.

    Returns (devices_csv, error_message).
    On success error_message is None. On failure devices_csv is None.
    """
    free: list[int] = npu_info.get("free", [])
    busy: dict[str, list] = npu_info.get("busy", {})

    if requested_devices is not None:
        requested = [int(d.strip()) for d in requested_devices.split(",")]
        conflicts = [d for d in requested if str(d) in busy]
        if conflicts:
            details = {
                str(d): busy[str(d)] for d in conflicts if str(d) in busy
            }
            return None, (
                f"requested devices {conflicts} are busy: {json.dumps(details)}; "
                f"free devices: {free}"
            )
        return requested_devices, None

    if tp is None:
        return None, None

    need = tp * (dp or 1)
    if len(free) < need:
        return None, (
            f"need {need} free NPUs (tp={tp}, dp={dp or 1}) but only {len(free)} available; "
            f"free={free}, busy={list(busy.keys())}"
        )
    selected = free[:need]
    return ",".join(str(d) for d in selected), None


# ---------------------------------------------------------------------------
# Progress / output
# ---------------------------------------------------------------------------

def emit_progress(phase: str, message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"phase": phase, "message": message}
    payload.update({k: v for k, v in extra.items() if v is not None})
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def now_utc() -> str:
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
