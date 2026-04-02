from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Sequence

from .config import RepoPaths

CAPABILITY_STATE_SCHEMA_VERSION = 3
RETIRED_STATE_KEYS = {
    "lifecycle",
    "requested_mode",
    "current_target",
    "current_target_kind",
    "server_verifications",
    "runtime",
    "bootstrap",
}


def default_capability_state() -> dict[str, object]:
    return {
        "schema_version": CAPABILITY_STATE_SCHEMA_VERSION,
        "servers": {},
        "services": {},
        "benchmark_runs": {},
    }


def _write_state_atomic(state_file: os.PathLike[str] | str, state: Dict[str, Any]) -> None:
    path = os.fspath(state_file)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=2) + "\n")
    os.replace(tmp_path, path)


@contextmanager
def locked_capability_state(paths: RepoPaths, timeout_s: float = 5.0) -> Iterator[None]:
    lock_path = paths.state_lock_file
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    start = time.monotonic()
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() - start >= timeout_s:
                os.close(lock_fd)
                raise TimeoutError(f"timed out waiting for state lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _validate_capability_state(state: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise RuntimeError("invalid runtime state: .workspace.local/state.json")

    normalized = dict(state)
    schema_version = normalized.get("schema_version", CAPABILITY_STATE_SCHEMA_VERSION)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise RuntimeError("invalid runtime state: .workspace.local/state.json")
    if schema_version == 2:
        normalized["schema_version"] = CAPABILITY_STATE_SCHEMA_VERSION
        normalized.setdefault("services", {})
        normalized.setdefault("benchmark_runs", {})
    elif schema_version != CAPABILITY_STATE_SCHEMA_VERSION:
        raise RuntimeError(f"unsupported runtime state schema_version: {schema_version}")

    for key in RETIRED_STATE_KEYS:
        if key in normalized:
            raise RuntimeError(f"retired state key present: {key}")

    normalized["schema_version"] = CAPABILITY_STATE_SCHEMA_VERSION
    for top_level in (
        "servers",
        "services",
        "benchmark_runs",
        "code_parity",
        "runtime_environment",
    ):
        value = normalized.get(top_level)
        if value is None:
            if top_level in {"code_parity", "runtime_environment"}:
                continue
            normalized[top_level] = {}
        elif not isinstance(value, dict):
            raise RuntimeError(f"invalid runtime state: {top_level} must be an object")
    return normalized


def diagnose_state_residue(paths: RepoPaths) -> list[str]:
    residue: list[str] = []
    if not paths.local_state_file.exists():
        return residue
    try:
        payload = json.loads(paths.local_state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        residue.append("state.json is corrupted or unreadable")
        return residue
    if not isinstance(payload, dict):
        residue.append("state.json must be a JSON object")
        return residue

    schema_version = payload.get("schema_version")
    if schema_version not in {2, CAPABILITY_STATE_SCHEMA_VERSION}:
        residue.append(f"state.json has unsupported schema_version: {schema_version}")
    for key in RETIRED_STATE_KEYS:
        if key in payload:
            residue.append(f"state.json contains retired key: {key}")
    return residue


def read_capability_state(paths: RepoPaths) -> Dict[str, Any]:
    state_file = paths.local_state_file
    if not state_file.exists():
        return default_capability_state()

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid runtime state: .workspace.local/state.json") from exc

    if not isinstance(data, dict):
        raise RuntimeError("invalid runtime state: .workspace.local/state.json")
    return _validate_capability_state(data)


def write_capability_state(paths: RepoPaths, state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _validate_capability_state(state)
    paths.local_state_file.parent.mkdir(parents=True, exist_ok=True)
    _write_state_atomic(paths.local_state_file, normalized)
    return normalized


def write_capability_leaf(
    paths: RepoPaths,
    leaf_path: Sequence[str],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    with locked_capability_state(paths):
        state = read_capability_state(paths)
        cursor: Dict[str, Any] = state
        for key in leaf_path[:-1]:
            child = cursor.setdefault(key, {})
            if not isinstance(child, dict):
                child = {}
                cursor[key] = child
            cursor = child
        cursor[leaf_path[-1]] = dict(payload)
        return write_capability_state(paths, state)


def upsert_service_session(paths: RepoPaths, session: Dict[str, Any]) -> Dict[str, Any]:
    if "service_id" not in session:
        raise RuntimeError("service session missing service_id")
    return write_capability_leaf(paths, ("services", str(session["service_id"])), session)


def remove_service_session(paths: RepoPaths, service_id: str) -> Dict[str, Any]:
    with locked_capability_state(paths):
        state = read_capability_state(paths)
        services = state.get("services")
        if not isinstance(services, dict):
            services = {}
            state["services"] = services
        services.pop(service_id, None)
        return write_capability_state(paths, state)


def record_benchmark_run(paths: RepoPaths, run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return write_capability_leaf(paths, ("benchmark_runs", run_id), payload)
