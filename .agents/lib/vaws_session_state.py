#!/usr/bin/env python3
"""Session state and lease helpers for VAWS parallel agent sessions."""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from vaws_local_state import ROOT, STATE_DIR, WorkspaceStateError, ensure_state_dir, utc_now_iso
from vaws_session_id import load_current_session_binding, normalize_session_id
from vaws_validate import parse_device_csv

SESSION_SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1
LEASE_SCHEMA_VERSION = 1
SESSION_ROOT = STATE_DIR / "sessions"
SESSION_INDEX_PATH = SESSION_ROOT / "index.json"
SESSION_LEASES_PATH = SESSION_ROOT / "leases.json"
SESSION_LOCK_DIR = SESSION_ROOT / "locks"
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
DEFAULT_LOCK_POLL_SECONDS = 0.1
DEFAULT_STALE_LOCK_SECONDS = 60 * 60 * 6
DEFAULT_CONTAINER_SSH_PORT_RANGE = "46000:46999"
DEFAULT_SERVING_PORT_RANGE = "30000:45999"
SAFE_TOKEN_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class SessionStateError(WorkspaceStateError):
    """Raised for deterministic session-state failures."""


@dataclass(frozen=True)
class SessionLookup:
    session: dict[str, Any]
    session_file: Path
    state_repo_root: Path


def _atomic_write_json(path: Path, data: Any) -> None:
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


def _load_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SessionStateError(f"invalid JSON in {path}: {exc}") from exc


@contextlib.contextmanager
def file_lock(
    path: Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
    stale_after_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
):
    ensure_state_dir(path.parent)
    deadline = time.monotonic() + timeout_seconds
    owner = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": utc_now_iso(),
    }
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, json.dumps(owner, ensure_ascii=False).encode("utf-8"))
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age >= stale_after_seconds:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise SessionStateError(f"timed out waiting for session lock {path}")
            time.sleep(poll_seconds)
    try:
        yield path
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def sessions_root(repo_root: Path = ROOT) -> Path:
    return repo_root / ".vaws-local" / "sessions"


def session_index_path(repo_root: Path = ROOT) -> Path:
    return sessions_root(repo_root) / "index.json"


def session_leases_path(repo_root: Path = ROOT) -> Path:
    return sessions_root(repo_root) / "leases.json"


def session_lock_dir(repo_root: Path = ROOT) -> Path:
    return sessions_root(repo_root) / "locks"


def session_dir(session_id: str, repo_root: Path = ROOT) -> Path:
    normalized = require_session_id(session_id)
    return sessions_root(repo_root) / normalized


def session_file_path(session_id: str, repo_root: Path = ROOT) -> Path:
    return session_dir(session_id, repo_root) / "session.json"


def session_serving_state_path(session_id: str, repo_root: Path = ROOT) -> Path:
    return session_dir(session_id, repo_root) / "serving.json"


def session_benchmark_dir(session_id: str, repo_root: Path = ROOT) -> Path:
    return session_dir(session_id, repo_root) / "benchmark"


def require_session_id(value: str) -> str:
    normalized = normalize_session_id(value)
    if normalized is None:
        raise SessionStateError(f"invalid session id: {value!r}")
    return normalized


def safe_token(value: str, *, fallback: str = "item", max_len: int = 63) -> str:
    token = SAFE_TOKEN_PATTERN.sub("-", value.strip()).strip(".-_")
    if not token:
        token = fallback
    if len(token) <= max_len:
        return token
    digest = __import__("hashlib").sha1(token.encode("utf-8")).hexdigest()[:8]
    keep = max(1, max_len - len(digest) - 1)
    return f"{token[:keep].rstrip('.-_')}-{digest}"


def default_worktree_root(repo_root: Path, session_id: str) -> Path:
    return repo_root.parent / "vaws-worktrees" / repo_root.name / require_session_id(session_id)


def default_branch(session_id: str) -> str:
    return f"session/{require_session_id(session_id)}"


def session_container_name(namespace: str | None, session_id: str) -> str:
    ns = safe_token(namespace or "agent", fallback="agent", max_len=24)
    sid = safe_token(require_session_id(session_id), fallback="session", max_len=40)
    return safe_token(f"vaws-{ns}-{sid}", fallback="vaws-session", max_len=63)


def parse_port_range(value: str) -> tuple[int, int]:
    start_s, sep, end_s = value.partition(":")
    if not sep:
        raise SessionStateError(f"port range must be START:END, got {value!r}")
    start = int(start_s)
    end = int(end_s)
    if start <= 0 or end <= 0 or start > end or end > 65535:
        raise SessionStateError(f"invalid port range: {value!r}")
    return start, end


def _empty_index() -> dict[str, Any]:
    return {"schema_version": INDEX_SCHEMA_VERSION, "updated_at": utc_now_iso(), "sessions": {}}


def _empty_leases() -> dict[str, Any]:
    return {"schema_version": LEASE_SCHEMA_VERSION, "updated_at": utc_now_iso(), "leases": {}}


def load_index(repo_root: Path = ROOT) -> dict[str, Any]:
    data = _load_json(session_index_path(repo_root), _empty_index())
    if not isinstance(data, dict) or data.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise SessionStateError("unsupported sessions index schema")
    data.setdefault("sessions", {})
    if not isinstance(data["sessions"], dict):
        raise SessionStateError("sessions index must contain a sessions object")
    return data


def save_index(index: dict[str, Any], repo_root: Path = ROOT) -> Path:
    index["schema_version"] = INDEX_SCHEMA_VERSION
    index["updated_at"] = utc_now_iso()
    path = session_index_path(repo_root)
    _atomic_write_json(path, index)
    return path


def load_leases(repo_root: Path = ROOT) -> dict[str, Any]:
    data = _load_json(session_leases_path(repo_root), _empty_leases())
    if not isinstance(data, dict) or data.get("schema_version") != LEASE_SCHEMA_VERSION:
        raise SessionStateError("unsupported sessions lease schema")
    data.setdefault("leases", {})
    if not isinstance(data["leases"], dict):
        raise SessionStateError("sessions leases must contain a leases object")
    return data


def save_leases(leases: dict[str, Any], repo_root: Path = ROOT) -> Path:
    leases["schema_version"] = LEASE_SCHEMA_VERSION
    leases["updated_at"] = utc_now_iso()
    path = session_leases_path(repo_root)
    _atomic_write_json(path, leases)
    return path


def _machine_lease_bucket(leases: dict[str, Any], machine_alias: str) -> dict[str, Any]:
    bucket = leases.setdefault("leases", {}).setdefault(machine_alias, {})
    bucket.setdefault("npu_devices", {})
    bucket.setdefault("container_ssh_ports", {})
    bucket.setdefault("service_ports", {})
    return bucket


def _resource_owner(bucket: dict[str, Any], kind: str, value: str) -> str | None:
    record = bucket.get(kind, {}).get(value)
    if isinstance(record, dict):
        owner = record.get("session_id")
        return str(owner) if owner is not None else None
    return None


def _reserve(bucket: dict[str, Any], kind: str, value: int | str, session_id: str) -> None:
    key = str(value)
    owner = _resource_owner(bucket, kind, key)
    if owner is not None and owner != session_id:
        raise SessionStateError(f"{kind[:-1]} {key} is already leased by session {owner}")
    bucket.setdefault(kind, {})[key] = {"session_id": session_id, "updated_at": utc_now_iso()}


def _select_port(
    bucket: dict[str, Any],
    kind: str,
    session_id: str,
    port_range: str,
    *,
    preferred: int | None = None,
    is_available: Callable[[int], bool] | None = None,
) -> int:
    start, end = parse_port_range(port_range)
    candidates: Iterable[int] = [preferred] if preferred is not None else range(start, end + 1)
    for port in candidates:
        if port is None:
            continue
        if port < start or port > end:
            raise SessionStateError(f"port {port} is outside allowed range {port_range}")
        owner = _resource_owner(bucket, kind, str(port))
        if owner is not None and owner != session_id:
            if preferred is not None:
                raise SessionStateError(f"port {port} is already leased by session {owner}")
            continue
        if is_available is not None and not is_available(port):
            if preferred is not None:
                raise SessionStateError(f"port {port} is not available on the remote host/container")
            continue
        _reserve(bucket, kind, port, session_id)
        return port
    raise SessionStateError(f"no free port found in range {port_range}")


def allocate_session_leases(
    *,
    repo_root: Path = ROOT,
    machine_alias: str,
    session_id: str,
    requested_devices: list[int] | None = None,
    npu_count: int | None = None,
    available_devices: list[int] | None = None,
    container_ssh_port: int | None = None,
    container_ssh_port_range: str = DEFAULT_CONTAINER_SSH_PORT_RANGE,
    port_available: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    sid = require_session_id(session_id)
    if npu_count is not None and npu_count < 1:
        raise SessionStateError("--npu-count must be >= 1")
    if requested_devices is not None:
        requested_devices = parse_device_csv(",".join(str(item) for item in requested_devices)) or []
    if requested_devices is not None and npu_count is not None:
        raise SessionStateError("use only one of --devices or --npu-count")
    available_set = set(available_devices) if available_devices is not None else None
    with file_lock(session_lock_dir(repo_root) / "leases.lock"):
        leases = load_leases(repo_root)
        bucket = _machine_lease_bucket(leases, machine_alias)
        allocated_devices: list[int] = []
        if requested_devices is not None:
            if available_set is not None:
                missing = sorted(set(requested_devices) - available_set)
                if missing:
                    raise SessionStateError(
                        f"requested NPU devices are not visible on host: {missing}; "
                        f"available={sorted(available_set)}"
                    )
            allocated_devices = list(requested_devices)
        elif npu_count:
            candidates = sorted(available_set) if available_set is not None else list(range(64))
            for dev in candidates:
                if _resource_owner(bucket, "npu_devices", str(dev)) in {None, sid}:
                    allocated_devices.append(dev)
                if len(allocated_devices) >= npu_count:
                    break
            if len(allocated_devices) < npu_count:
                raise SessionStateError(f"not enough locally unleased NPU devices for session {sid}")

        for dev in allocated_devices:
            _reserve(bucket, "npu_devices", dev, sid)
        port = _select_port(
            bucket,
            "container_ssh_ports",
            sid,
            container_ssh_port_range,
            preferred=container_ssh_port,
            is_available=port_available,
        )
        save_leases(leases, repo_root)
    return {"npu_devices": allocated_devices, "container_ssh_port": port}


def allocate_service_port(
    *,
    repo_root: Path = ROOT,
    machine_alias: str,
    session_id: str,
    requested_port: int | None = None,
    serving_port_range: str = DEFAULT_SERVING_PORT_RANGE,
    port_available: Callable[[int], bool] | None = None,
) -> int:
    sid = require_session_id(session_id)
    with file_lock(session_lock_dir(repo_root) / "leases.lock"):
        leases = load_leases(repo_root)
        bucket = _machine_lease_bucket(leases, machine_alias)
        port = _select_port(
            bucket,
            "service_ports",
            sid,
            serving_port_range,
            preferred=requested_port,
            is_available=port_available,
        )
        save_leases(leases, repo_root)
    return port


def release_service_port(
    *,
    repo_root: Path = ROOT,
    machine_alias: str,
    session_id: str,
    port: int | None,
) -> None:
    if port is None:
        return
    sid = require_session_id(session_id)
    with file_lock(session_lock_dir(repo_root) / "leases.lock"):
        leases = load_leases(repo_root)
        bucket = _machine_lease_bucket(leases, machine_alias)
        record = bucket.get("service_ports", {}).get(str(port))
        if isinstance(record, dict) and record.get("session_id") == sid:
            bucket["service_ports"].pop(str(port), None)
            save_leases(leases, repo_root)


def release_all_session_leases(*, repo_root: Path = ROOT, session_id: str) -> None:
    sid = require_session_id(session_id)
    with file_lock(session_lock_dir(repo_root) / "leases.lock"):
        leases = load_leases(repo_root)
        for bucket in leases.get("leases", {}).values():
            for kind in ("npu_devices", "container_ssh_ports", "service_ports"):
                records = bucket.get(kind, {})
                for key, record in list(records.items()):
                    if isinstance(record, dict) and record.get("session_id") == sid:
                        records.pop(key, None)
        save_leases(leases, repo_root)


def validate_session(session: dict[str, Any], *, where: str = "session") -> dict[str, Any]:
    if not isinstance(session, dict):
        raise SessionStateError(f"{where} must be an object")
    if session.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise SessionStateError(f"unsupported {where}.schema_version: {session.get('schema_version')!r}")
    sid = require_session_id(str(session.get("session_id", "")))
    if not isinstance(session.get("base_machine"), str) or not session["base_machine"]:
        raise SessionStateError(f"{where}.base_machine must be a non-empty string")
    if not isinstance(session.get("local"), dict):
        raise SessionStateError(f"{where}.local must be an object")
    if not isinstance(session.get("remote"), dict):
        raise SessionStateError(f"{where}.remote must be an object")
    normalized = dict(session)
    normalized["session_id"] = sid
    return normalized


def save_session(session: dict[str, Any], *, repo_root: Path = ROOT) -> Path:
    normalized = validate_session(session)
    sid = normalized["session_id"]
    now = utc_now_iso()
    normalized.setdefault("created_at", now)
    normalized["updated_at"] = now
    path = session_file_path(sid, repo_root)
    with file_lock(session_lock_dir(repo_root) / f"{sid}.lock"):
        _atomic_write_json(path, normalized)
        with file_lock(session_lock_dir(repo_root) / "index.lock"):
            index = load_index(repo_root)
            index.setdefault("sessions", {})[sid] = {
                "session_id": sid,
                "base_machine": normalized["base_machine"],
                "status": normalized.get("status", "ready"),
                "created_at": normalized.get("created_at"),
                "updated_at": normalized.get("updated_at"),
                "session_file": str(path.relative_to(repo_root)),
            }
            save_index(index, repo_root)
    return path


def _session_file_from_binding(repo_root: Path, session_id: str | None) -> Path | None:
    binding = load_current_session_binding(repo_root)
    if not binding:
        return None
    bound_id = normalize_session_id(str(binding.get("session_id", "")))
    if session_id is not None and bound_id != require_session_id(session_id):
        return None
    session_file = binding.get("session_file")
    if isinstance(session_file, str) and session_file:
        return Path(session_file).expanduser().resolve()
    base_repo_root = binding.get("base_repo_root")
    if isinstance(base_repo_root, str) and base_repo_root:
        return session_file_path(bound_id or require_session_id(session_id or ""), Path(base_repo_root))
    return None


def load_session_lookup(
    *,
    session_id: str | None = None,
    session_file: str | Path | None = None,
    repo_root: Path = ROOT,
) -> SessionLookup:
    sid = require_session_id(session_id) if session_id else None
    if session_file is not None:
        path = Path(session_file).expanduser().resolve()
    else:
        path = None
        if sid is not None:
            index = load_index(repo_root)
            record = index.get("sessions", {}).get(sid)
            if isinstance(record, dict) and isinstance(record.get("session_file"), str):
                candidate = Path(record["session_file"])
                path = candidate if candidate.is_absolute() else repo_root / candidate
            else:
                candidate = session_file_path(sid, repo_root)
                if candidate.exists():
                    path = candidate
        if path is None:
            path = _session_file_from_binding(repo_root, sid)
        if path is None and sid is None:
            binding = load_current_session_binding(repo_root)
            if binding and isinstance(binding.get("session_id"), str):
                sid = require_session_id(binding["session_id"])
                path = _session_file_from_binding(repo_root, sid) or session_file_path(sid, repo_root)
        if path is None:
            raise SessionStateError("session id or session file is required")
    if not path.exists():
        raise SessionStateError(f"session file does not exist: {path}")
    session = validate_session(_load_json(path), where=str(path))
    if sid is not None and session["session_id"] != sid:
        raise SessionStateError(f"session file {path} contains {session['session_id']!r}, expected {sid!r}")
    state_root = path.parents[3] if path.parent.parent.name == "sessions" else repo_root
    return SessionLookup(session=session, session_file=path, state_repo_root=state_root)


def load_session(
    *,
    session_id: str | None = None,
    session_file: str | Path | None = None,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    return load_session_lookup(session_id=session_id, session_file=session_file, repo_root=repo_root).session


def mark_session_status(
    *,
    repo_root: Path = ROOT,
    session_id: str,
    status: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lookup = load_session_lookup(session_id=session_id, repo_root=repo_root)
    session = dict(lookup.session)
    session["status"] = status
    session["updated_at"] = utc_now_iso()
    if extra:
        session.update(extra)
    save_session(session, repo_root=lookup.state_repo_root)
    return session


def session_record_for_execution(session: dict[str, Any]) -> dict[str, Any]:
    remote = session["remote"]
    container = remote["container"]
    return {
        "alias": session["base_machine"],
        "namespace": remote.get("namespace"),
        "host": {
            "ip": remote["host"],
            "port": remote.get("host_port", 22),
            "user": remote.get("host_user", "root"),
            "machine_type": remote.get("machine_type"),
            "soc": remote.get("soc"),
        },
        "container": {
            "name": container["name"],
            "ssh_port": container["ssh_port"],
            "image": container.get("image", ""),
            "workdir": container.get("workdir", "/vllm-workspace"),
            "machine_type": container.get("machine_type") or remote.get("machine_type"),
        },
        "bootstrap_method": "ssh",
        "managed_by_skill": True,
        "created_by_skill": True,
    }
