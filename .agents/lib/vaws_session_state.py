"""Local business reports for one VAWS task. Resource ownership is the coordinator."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from vaws_local_state import ROOT, WorkspaceStateError, ensure_state_dir
from vaws_session_id import normalize_session_id

SAFE_TOKEN_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class SessionStateError(WorkspaceStateError):
    """Raised for deterministic local-state failures."""


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


def task_state_root(repo_root: Path = ROOT) -> Path:
    """Worktree-local business reports for one VAWS task. Not a resource allocator."""
    return repo_root / ".vaws-local" / "tasks"


def require_task_id(value: str) -> str:
    normalized = normalize_session_id(value)
    if normalized is None:
        raise SessionStateError(f"invalid task id: {value!r}")
    return normalized


def task_dir(task_id: str, repo_root: Path = ROOT) -> Path:
    return task_state_root(repo_root) / require_task_id(task_id)


def serving_state_path(task_id: str, repo_root: Path = ROOT, *, service: str = "vllm") -> Path:
    if not isinstance(service, str) or not service.strip():
        raise SessionStateError("service must be a nonempty string")
    digest = hashlib.sha256(service.encode("utf-8")).hexdigest()
    return task_dir(task_id, repo_root) / "serving" / f"{digest}.json"


def benchmark_dir(task_id: str, repo_root: Path = ROOT) -> Path:
    return task_dir(task_id, repo_root) / "benchmark"


def safe_token(value: str, *, fallback: str = "item", max_len: int = 63) -> str:
    token = SAFE_TOKEN_PATTERN.sub("-", value.strip()).strip(".-_")
    if not token:
        token = fallback
    if len(token) <= max_len:
        return token
    digest = __import__("hashlib").sha1(token.encode("utf-8")).hexdigest()[:8]
    keep = max(1, max_len - len(digest) - 1)
    return f"{token[:keep].rstrip('.-_')}-{digest}"


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SessionStateError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SessionStateError(f"{path} must contain a JSON object")
    return data


def load_serving_state(task_id: str, *, service: str = "vllm", repo_root: Path = ROOT) -> dict[str, Any] | None:
    """Human business launch config. Not an execution recovery receipt."""
    path = serving_state_path(task_id, repo_root, service=service)
    if not path.exists():
        path = task_dir(task_id, repo_root) / "serving.json"
        if not path.exists():
            return None
    try:
        data = load_json_object(path)
        return data if data.get("service", "vllm") == service else None
    except SessionStateError as exc:
        raise SessionStateError(
            f"serving report is unreadable: {path} ({exc}); inspect the running "
            "service, then delete this file to reset the record"
        ) from exc


def save_serving_state(task_id: str, data: dict[str, Any], *, repo_root: Path = ROOT) -> Path:
    path = serving_state_path(task_id, repo_root, service=data.get("service", "vllm"))
    _atomic_write_json(path, data)
    return path


def write_json(path: Path, data: Any) -> Path:
    _atomic_write_json(path, data)
    return path
