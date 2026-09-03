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
from vaws_npu_coordination import parse_npu_smi_info  # noqa: E402
from vaws_remote_toolbox import (  # noqa: E402
    SshEndpoint,
    ascend_env_preamble,
    resolve_remote_target,
)
from vaws_session_state import session_serving_state_path  # noqa: E402
from vaws_ssh import base_ssh_options  # noqa: E402
from vaws_validate import parse_device_csv  # noqa: E402

PROGRESS_SENTINEL = "__VAWS_SERVING_PROGRESS__="

# Always bound the TCP connect phase: without it a dead host can hang an SSH
# command for minutes (kernel default). Established connections are unaffected
# by ConnectTimeout.
SSH_CONNECT_TIMEOUT_SECONDS = 15
# Hard cap for a single SSH round-trip. Must exceed the slowest remote probe
# (first-token curl allows --max-time 120).
SSH_EXEC_DEFAULT_TIMEOUT_SECONDS = 180


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionTarget:
    mode: str
    alias: str
    session_id: str | None
    endpoint: SshEndpoint
    host_endpoint: SshEndpoint
    runtime_base: str
    record: dict[str, Any]
    state_repo_root: Path
    session_file: Path | None = None
    session: dict[str, Any] | None = None


def _ssh_base_cmd(endpoint: SshEndpoint) -> list[str]:
    return [
        "ssh",
        *base_ssh_options(connect_timeout=SSH_CONNECT_TIMEOUT_SECONDS),
        "-p", str(endpoint.port),
        endpoint.destination(),
    ]


def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    timeout: float | None = SSH_EXEC_DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    # Serving keeps its own copy so the serving surface stays untouched;
    # vaws_remote_toolbox.ssh_exec is the equivalent shared implementation
    # for new code.
    cmd = [*_ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(script)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # A timed-out probe is an unknown result (same shape as a lost SSH
        # connection, rc 255), never proof of remote success or failure.
        result = subprocess.CompletedProcess(
            cmd, 255, "", f"ssh_exec timed out after {exc.timeout}s"
        )
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
        port=record["host"].get("port", record["host"].get("ssh_port", 22)),
        user=record["host"].get("user", "root"),
    )


def resolve_execution_target(
    *,
    session_id: str | None = None,
    session_file: str | Path | None = None,
) -> ExecutionTarget:
    """Resolve the session execution target.

    Serving is session-only: with no explicit id/file the session is
    auto-resolved from the nearest worktree binding (cwd upward).
    """
    remote = resolve_remote_target(
        session_id=session_id,
        session_file=session_file,
        repo_root=ROOT,
    )
    return ExecutionTarget(
        mode=remote.mode,
        alias=remote.alias,
        session_id=remote.session_id,
        endpoint=remote.container_endpoint,
        host_endpoint=remote.host_endpoint,
        runtime_base=remote.runtime_root,
        record=remote.record,
        state_repo_root=remote.state_repo_root,
        session_file=remote.session_file,
        session=remote.session,
    )


# ---------------------------------------------------------------------------
# Local serving state
# ---------------------------------------------------------------------------

def load_serving_state(
    session_id: str,
    *,
    state_repo_root: Path = ROOT,
) -> dict[str, Any] | None:
    path = session_serving_state_path(session_id, state_repo_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt state file is NOT the same as "no service": treating it as
        # None could double-launch or leave an old vllm process untracked.
        raise RuntimeError(
            f"serving state file is unreadable: {path} ({exc}); inspect the running "
            f"service manually, then delete this file to reset the record"
        ) from exc


def save_serving_state(
    session_id: str,
    data: dict[str, Any],
    *,
    state_repo_root: Path = ROOT,
) -> Path:
    path = session_serving_state_path(session_id, state_repo_root)
    ensure_state_dir(path.parent)
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
# Serving presets
# ---------------------------------------------------------------------------

PRESETS_DIR = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "presets"


def load_preset(name: str) -> dict[str, Any]:
    """Load a named serving preset from the skill's ``presets/`` directory.

    ``name`` is a bare preset name (the ``.json`` suffix is optional); path
    traversal is rejected. Raises ``ValueError`` for unknown presets or
    malformed preset files.
    """
    stem = name[:-5] if name.endswith(".json") else name
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        raise ValueError(f"invalid preset name {name!r}: use a bare preset name")
    path = PRESETS_DIR / f"{stem}.json"
    if not path.is_file():
        available = sorted(p.stem for p in PRESETS_DIR.glob("*.json")) if PRESETS_DIR.is_dir() else []
        raise ValueError(
            f"unknown preset {name!r}; available presets: "
            + (", ".join(available) if available else "(none)")
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"preset {name!r} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"preset {name!r} must contain a JSON object")
    return data


# ---------------------------------------------------------------------------
# NPU probe
# ---------------------------------------------------------------------------

def probe_npus(host_ep: SshEndpoint) -> dict[str, Any]:
    """Probe NPU device availability via the **host** (bare-metal) SSH.

    Running npu-smi on the host (not inside a container) is critical because
    the host kernel can see processes from ALL containers.  Inside a single
    container, PID-namespace isolation hides other containers' workloads,
    making process-based occupancy detection unreliable.

    Parsing reuses the coordinator's single-source ``parse_npu_smi_info``,
    which maps A3 pipe-separated process rows through Phy-ID and fails closed
    when the process table is missing or unparsable.  A failed parse raises
    here as well: an unknown occupancy must never look like an empty busy set.
    As a secondary signal, HBM usage above the parser's
    ``hbm_busy_threshold_mb`` marks a device as busy even when no visible PID
    is found (covers edge cases where npu-smi does not list the process).
    """
    result = ssh_exec(host_ep, "npu-smi info", check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"npu-smi on host failed (rc={result.returncode}): "
            f"{result.stderr[:500]}"
        )
    parsed = parse_npu_smi_info(result.stdout)
    if parsed.get("status") != "ok":
        raise RuntimeError(
            "npu-smi occupancy parse failed; refusing to treat unknown "
            f"occupancy as free: {parsed.get('error', 'unknown parse error')}"
        )
    parsed["total"] = len(parsed["devices"])
    parsed["free_count"] = len(parsed["free"])
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
        requested = parse_device_csv(requested_devices) or []
        visible = set(npu_info.get("devices", []))
        missing = [d for d in requested if d not in visible]
        if missing:
            return None, (
                f"requested devices {missing} are not visible on host; "
                f"visible={sorted(visible)}"
            )
        conflicts = [d for d in requested if str(d) in busy]
        if conflicts:
            details = {
                str(d): busy[str(d)] for d in conflicts if str(d) in busy
            }
            return None, (
                f"requested devices {conflicts} are busy: {json.dumps(details)}; "
                f"free devices: {free}"
            )
        return ",".join(str(d) for d in requested), None

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
