#!/usr/bin/env python3
"""Session state helpers. Resource ownership is the host coordinator."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from vaws_local_state import ROOT, STATE_DIR, WorkspaceStateError, ensure_state_dir, utc_now_iso
from vaws_session_id import find_session_binding, load_current_session_binding, normalize_session_id
from vaws_validate import parse_device_csv

SESSION_SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1
SESSION_ROOT = STATE_DIR / "sessions"
SESSION_INDEX_PATH = SESSION_ROOT / "index.json"
SESSION_LOCK_DIR = SESSION_ROOT / "locks"
UNSUPPORTED_RECEIPT = (
    "session has no coordinator receipt; recreate the session or reconcile host reservations"
)
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


class _FlockUnsupported(Exception):
    """Raised when fcntl.flock is not supported on this filesystem."""


_GUARD_THREAD_LOCKS: dict[str, threading.Lock] = {}
_GUARD_THREAD_LOCKS_MU = threading.Lock()
_FLOCK_UNSUPPORTED_ERRNOS = {
    errno.ENOTSUP,
    errno.ENOSYS,
    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
}
_FLOCK_BUSY_ERRNOS = {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}


def _guard_path(lock_path: Path) -> Path:
    return lock_path.with_name(f"{lock_path.name}.guard")


def _thread_gate(lock_path: Path) -> threading.Lock:
    key = os.path.normpath(str(lock_path))
    with _GUARD_THREAD_LOCKS_MU:
        gate = _GUARD_THREAD_LOCKS.get(key)
        if gate is None:
            gate = threading.Lock()
            _GUARD_THREAD_LOCKS[key] = gate
        return gate


def _timed_out(path: Path) -> SessionStateError:
    return SessionStateError(f"timed out waiting for session lock {path}")


@contextlib.contextmanager
def _reclaim_gate(lock_path: Path, *, deadline: float, poll_seconds: float):
    """Serialize reclaim/release on a never-unlinked sidecar.

    The lease file stays an O_EXCL lock. This gate only covers unlink of
    that file. A per-path threading.Lock covers same-process waiters if
    flock is process-scoped; fcntl.flock covers other processes on local
    POSIX filesystems. NFS and cross-host coherence are out of scope.
    ENOTSUP fails closed for reclaim.
    """
    guard = _guard_path(lock_path)
    ensure_state_dir(guard.parent)
    thread_gate = _thread_gate(lock_path)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timed_out(lock_path)
        if thread_gate.acquire(timeout=min(poll_seconds, remaining)):
            break
    fd: int | None = None
    locked = False
    try:
        fd = os.open(str(guard), os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno in _FLOCK_UNSUPPORTED_ERRNOS:
                    raise _FlockUnsupported() from exc
                if exc.errno not in _FLOCK_BUSY_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise _timed_out(lock_path)
                time.sleep(poll_seconds)
        try:
            yield
        finally:
            if locked:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            thread_gate.release()


def _lock_identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _lock_is_stale(path: Path, stale_after_seconds: float) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return (time.time() - st.st_mtime) >= stale_after_seconds


def _reclaim_stale_lock(path: Path, stale_after_seconds: float) -> None:
    """Unlink a stale lease. Caller must hold ``_reclaim_gate``."""
    identity = _lock_identity(path)
    if identity is None or not _lock_is_stale(path, stale_after_seconds):
        return
    current = _lock_identity(path)
    if current != identity or not _lock_is_stale(path, stale_after_seconds):
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> None:
    """Unlink ``path`` only if it is still ``identity``. Caller must hold ``_reclaim_gate``."""
    if _lock_identity(path) != identity:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return


def _release_acquired_lease(
    path: Path,
    fd: int | None,
    identity: tuple[int, int] | None,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    pending_exc: BaseException | None,
) -> None:
    """Close the lease fd and unlink our inode under the cooperating guard.

    Gate timeout and unsupported flock fail closed: the lease is left in
    place. Those failures surface only when the critical section itself
    succeeded, so a body exception is not replaced by a cleanup timeout.
    Unrelated I/O errors, including lease-fd close failures, are not
    swallowed even if the body already failed; they chain to the body
    exception. The lease fd is closed even if gated unlink raises.
    """
    if fd is None and identity is None:
        return
    if fd is not None and identity is None:
        try:
            st = os.fstat(fd)
            identity = (st.st_dev, st.st_ino)
        except OSError:
            identity = None
    gate_exc: BaseException | None = None
    close_exc: OSError | None = None
    try:
        if identity is not None:
            release_deadline = time.monotonic() + timeout_seconds
            try:
                with _reclaim_gate(
                    path, deadline=release_deadline, poll_seconds=poll_seconds
                ):
                    _unlink_if_identity(path, identity)
            except _FlockUnsupported as exc:
                gate_exc = SessionStateError(
                    f"session lock reclaim is unsupported for {path}"
                )
                gate_exc.__cause__ = exc
            except SessionStateError as exc:
                gate_exc = exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError as exc:
                close_exc = exc
    if close_exc is not None:
        if pending_exc is not None:
            raise close_exc from pending_exc
        raise close_exc
    if pending_exc is not None:
        return
    if gate_exc is not None:
        raise gate_exc


@contextlib.contextmanager
def file_lock(
    path: Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
    stale_after_seconds: float = DEFAULT_STALE_LOCK_SECONDS,
):
    """Acquire an O_EXCL lease file, reclaiming a stale holder safely.

    Reclaim and release are serialized under ``{name}.guard``. A waiter
    must re-check inode and mtime under that gate before unlink, and a
    holder unlinks only its own inode. If the gate cannot be obtained,
    the lease is left in place and the failure is raised; release never
    falls back to an unguarded unlink. This is atomic on local POSIX
    filesystems for threads and processes that honor the gate. It does
    not claim NFS or cross-host lock coherence; if flock is ENOTSUP,
    reclaim and release fail closed and the lease is left in place.
    """
    ensure_state_dir(path.parent)
    deadline = time.monotonic() + timeout_seconds
    owner = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": utc_now_iso(),
    }
    fd: int | None = None
    identity: tuple[int, int] | None = None
    pending_exc: BaseException | None = None
    try:
        while True:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, json.dumps(owner, ensure_ascii=False).encode("utf-8"))
                st = os.fstat(fd)
                identity = (st.st_dev, st.st_ino)
                break
            except FileExistsError:
                try:
                    # path.stat() is the unguarded verdict; reclaim re-checks
                    # with lstat under the gate so a stale reading cannot
                    # unlink a newer holder's inode.
                    age = time.time() - path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age >= stale_after_seconds:
                    try:
                        with _reclaim_gate(
                            path, deadline=deadline, poll_seconds=poll_seconds
                        ):
                            _reclaim_stale_lock(path, stale_after_seconds)
                    except _FlockUnsupported:
                        if time.monotonic() >= deadline:
                            raise _timed_out(path)
                        time.sleep(poll_seconds)
                    continue
                if time.monotonic() >= deadline:
                    raise _timed_out(path)
                time.sleep(poll_seconds)
        try:
            yield path
        except BaseException as exc:
            pending_exc = exc
            raise
    finally:
        if fd is not None or identity is not None:
            _release_acquired_lease(
                path,
                fd,
                identity,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
                pending_exc=pending_exc,
            )


def sessions_root(repo_root: Path = ROOT) -> Path:
    return repo_root / ".vaws-local" / "sessions"


def session_index_path(repo_root: Path = ROOT) -> Path:
    return sessions_root(repo_root) / "index.json"


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


def session_receipt(session: dict[str, Any]) -> dict[str, Any]:
    receipt = (session.get("leases") or {}).get("receipt")
    if not isinstance(receipt, dict):
        raise SessionStateError(UNSUPPORTED_RECEIPT)
    if not receipt.get("task_id") or receipt.get("fence_token") is None or not receipt.get("coordination_epoch"):
        raise SessionStateError(UNSUPPORTED_RECEIPT)
    if not receipt.get("workspace_id") or not receipt.get("session_id"):
        raise SessionStateError(UNSUPPORTED_RECEIPT)
    return receipt


def host_endpoint_from_session(session: dict[str, Any]) -> dict[str, Any]:
    remote = session.get("remote") or {}
    host = remote.get("host")
    if not host:
        raise SessionStateError("session is missing a host endpoint")
    return {
        "host": host,
        "port": int(remote.get("host_port", 22)),
        "user": str(remote.get("host_user") or "root"),
    }


def session_resource_client(host_endpoint: Any, *, run=None, state_dir: str | None = None):
    from vaws_coordinator.host_queue import HostQueue
    from vaws_coordinator.session_resources import SessionResourceClient, remote_dev_host_run

    return SessionResourceClient(
        HostQueue(run or remote_dev_host_run),
        host_endpoint,
        state_dir=state_dir,
    )


def _client_error(exc: BaseException) -> SessionStateError:
    from vaws_coordinator.session_resources import SessionResourceError

    if isinstance(exc, SessionResourceError):
        return SessionStateError(str(exc))
    return SessionStateError(str(exc))


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


def allocate_session_leases(
    *,
    host_endpoint: Any,
    workspace_id: str,
    session_id: str,
    container_name: str | None = None,
    requested_devices: list[int] | None = None,
    npu_count: int | None = None,
    container_ssh_port: int | None = None,
    container_ssh_port_range: str = DEFAULT_CONTAINER_SSH_PORT_RANGE,
    run=None,
    agent_alias: str | None = None,
) -> dict[str, Any]:
    from vaws_coordinator.session_resources import receipt_from_reserve

    sid = require_session_id(session_id)
    if npu_count is not None and npu_count < 1:
        raise SessionStateError("--npu-count must be >= 1")
    if requested_devices is not None:
        requested_devices = parse_device_csv(",".join(str(item) for item in requested_devices)) or []
    if requested_devices is not None and npu_count is not None:
        raise SessionStateError("use only one of --devices or --npu-count")
    client = session_resource_client(host_endpoint, run=run)
    try:
        payload = client.reserve(
            workspace_id=workspace_id,
            session_id=sid,
            container_name=container_name,
            devices=requested_devices,
            npu_count=npu_count,
            container_ssh_port=container_ssh_port,
            container_ssh_port_range=container_ssh_port_range,
            agent_alias=agent_alias,
        )
    except Exception as exc:
        raise _client_error(exc) from exc
    receipt = receipt_from_reserve(payload, workspace_id=workspace_id, session_id=sid)
    return {
        "npu_devices": list(payload.get("npu_devices") or []),
        "container_ssh_port": payload.get("container_ssh_port"),
        "service_ports": list(payload.get("service_ports") or []),
        "receipt": receipt,
        "payload": payload,
    }


def allocate_service_port(
    *,
    session: dict[str, Any],
    host_endpoint: Any | None = None,
    requested_port: int | None = None,
    serving_port_range: str = DEFAULT_SERVING_PORT_RANGE,
    run=None,
) -> int:
    receipt = session_receipt(session)
    client = session_resource_client(
        host_endpoint or host_endpoint_from_session(session),
        run=run,
        state_dir=receipt.get("state_dir"),
    )
    try:
        payload = client.reserve_service_port(
            task_id=receipt["task_id"],
            fence_token=int(receipt["fence_token"]),
            coordination_epoch=str(receipt["coordination_epoch"]),
            workspace_id=str(receipt["workspace_id"]),
            session_id=str(receipt["session_id"]),
            requested_port=requested_port,
            serving_port_range=serving_port_range,
        )
    except Exception as exc:
        raise _client_error(exc) from exc
    port = int(payload.get("port") or payload.get("container_ssh_port") or 0)
    if port <= 0:
        raise SessionStateError("host did not return a service port")
    leases = dict(session.get("leases") or {})
    ports = [int(item) for item in (leases.get("service_ports") or [])]
    if port not in ports:
        ports.append(port)
    leases["service_ports"] = ports
    session["leases"] = leases
    return port


def release_service_port(
    *,
    session: dict[str, Any] | None = None,
    port: int | None = None,
    host_endpoint: Any | None = None,
    run=None,
    repo_root: Path | None = None,
    machine_alias: str | None = None,
    session_id: str | None = None,
) -> None:
    del repo_root, machine_alias
    if port is None:
        return
    if session is None:
        raise SessionStateError(UNSUPPORTED_RECEIPT)
    receipt = session_receipt(session)
    client = session_resource_client(
        host_endpoint or host_endpoint_from_session(session),
        run=run,
        state_dir=receipt.get("state_dir"),
    )
    try:
        client.release_service_port(
            task_id=receipt["task_id"],
            fence_token=int(receipt["fence_token"]),
            coordination_epoch=str(receipt["coordination_epoch"]),
            workspace_id=str(receipt["workspace_id"]),
            session_id=str(session_id or receipt["session_id"]),
            port=int(port),
        )
    except Exception as exc:
        raise _client_error(exc) from exc
    leases = dict(session.get("leases") or {})
    leases["service_ports"] = [
        int(item) for item in (leases.get("service_ports") or []) if int(item) != int(port)
    ]
    session["leases"] = leases


def session_live_leases(
    *,
    session: dict[str, Any] | None = None,
    repo_root: Path = ROOT,
    machine_alias: str | None = None,
    session_id: str | None = None,
) -> dict[str, list[int]]:
    del machine_alias
    if session is None:
        session = load_session_lookup(session_id=session_id, repo_root=repo_root).session
    leases = session.get("leases") or {}
    ssh_port = leases.get("container_ssh_port")
    return {
        "npu_devices": sorted(int(item) for item in (leases.get("npu_devices") or [])),
        "container_ssh_ports": [int(ssh_port)] if ssh_port else [],
        "service_ports": sorted(int(item) for item in (leases.get("service_ports") or [])),
    }


def require_session_npu_lease(session: dict[str, Any], *, repo_root: Path = ROOT) -> list[int]:
    """Return currently owned devices from the coordinator receipt snapshot."""
    del repo_root
    session_receipt(session)
    recorded = session.get("leases", {}).get("npu_devices", [])
    if not isinstance(recorded, list) or not recorded:
        raise SessionStateError(
            "managed NPU execution requires an active nonempty lease; reserve devices before launch"
        )
    return sorted(int(item) for item in recorded)


def release_all_session_leases(
    *,
    session: dict[str, Any] | None = None,
    host_endpoint: Any | None = None,
    run=None,
    repo_root: Path | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    if session is None:
        if session_id is None:
            raise SessionStateError(UNSUPPORTED_RECEIPT)
        session = load_session_lookup(session_id=session_id, repo_root=repo_root or ROOT).session
    receipt = session_receipt(session)
    container_name = ((session.get("remote") or {}).get("container") or {}).get("name")
    client = session_resource_client(
        host_endpoint or host_endpoint_from_session(session),
        run=run,
        state_dir=receipt.get("state_dir"),
    )
    try:
        return client.release(
            task_id=receipt["task_id"],
            fence_token=int(receipt["fence_token"]),
            coordination_epoch=str(receipt["coordination_epoch"]),
            workspace_id=str(receipt["workspace_id"]),
            session_id=str(receipt["session_id"]),
            container_name=container_name,
        )
    except Exception as exc:
        raise _client_error(exc) from exc


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


def _binding_session_file(binding: dict[str, Any] | None, session_id: str | None) -> Path | None:
    if not binding:
        return None
    bound_id = normalize_session_id(str(binding.get("session_id", "")))
    if bound_id is None:
        return None
    if session_id is not None and bound_id != require_session_id(session_id):
        return None
    session_file = binding.get("session_file")
    if isinstance(session_file, str) and session_file:
        return Path(session_file).expanduser().resolve()
    base_repo_root = binding.get("base_repo_root")
    if isinstance(base_repo_root, str) and base_repo_root:
        return session_file_path(bound_id, Path(base_repo_root))
    return None


def _session_file_from_binding(repo_root: Path, session_id: str | None) -> Path | None:
    return _binding_session_file(load_current_session_binding(repo_root), session_id)


def _candidate_bindings(repo_root: Path) -> list[dict[str, Any]]:
    """Bindings that can resolve a session, most specific first.

    The cwd-upward worktree binding wins over the repo-root binding so that a
    command run inside a session worktree always resolves to that session.
    """
    bindings: list[dict[str, Any]] = []
    found = find_session_binding()
    if found is not None:
        bindings.append(found[1])
    root_binding = load_current_session_binding(repo_root)
    if root_binding:
        bindings.append(root_binding)
    return bindings


def _candidate_state_roots(repo_root: Path, bindings: list[dict[str, Any]]) -> list[Path]:
    roots = [repo_root.expanduser().resolve()]
    for binding in bindings:
        base = binding.get("base_repo_root")
        if isinstance(base, str) and base:
            base_path = Path(base).expanduser().resolve()
            if base_path not in roots:
                roots.append(base_path)
    return roots


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
        bindings = _candidate_bindings(repo_root)
        if sid is not None:
            for state_root in _candidate_state_roots(repo_root, bindings):
                try:
                    index = load_index(state_root)
                except SessionStateError as exc:
                    # load_index degrades a missing index to an empty one, so
                    # reaching here means an existing index is corrupted.
                    # Degrade to the next candidate, but never silently.
                    print(f"session index under {state_root} is corrupted ({exc}); trying the next candidate",
                          file=sys.stderr)
                    continue
                record = index.get("sessions", {}).get(sid)
                if isinstance(record, dict) and isinstance(record.get("session_file"), str):
                    candidate = Path(record["session_file"])
                    path = candidate if candidate.is_absolute() else state_root / candidate
                    break
                candidate = session_file_path(sid, state_root)
                if candidate.exists():
                    path = candidate
                    break
        if path is None:
            for binding in bindings:
                candidate = _binding_session_file(binding, sid)
                if candidate is not None:
                    path = candidate
                    if sid is None:
                        sid = require_session_id(str(binding["session_id"]))
                    break
        if path is None:
            if sid is not None:
                raise SessionStateError(
                    f"session {sid!r} was not found in any known session index; "
                    "create it with session-management/scripts/session_create.py "
                    "or pass --session-file explicitly"
                )
            raise SessionStateError(
                "no session target: pass --session-id/--session-file, or run from "
                "inside a session worktree (a directory with .vaws-local/current-session.json). "
                "Create a session with session-management/scripts/session_create.py."
            )
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
    # Metadata transitions are not evidence that remote processes or ports
    # have disappeared. The cleanup caller releases leases only after proof.
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
