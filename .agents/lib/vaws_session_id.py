#!/usr/bin/env python3
"""Session-id resolution helpers for VAWS parallel agent sessions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vaws_local_state import STATE_DIR, WorkspaceStateError, utc_now_iso

SESSION_ID_PATTERN = re.compile(r"[^a-z0-9._-]+")
MULTI_DASH_PATTERN = re.compile(r"-+")
DEFAULT_SESSION_SOURCES_FILENAME = "session-id-sources.json"
CURRENT_SESSION_FILENAME = "current-session.json"
MAX_SESSION_ID_LENGTH = 64
SESSION_ID_HASH_LENGTH = 8


@dataclass(frozen=True)
class SessionId:
    value: str
    source: str


def normalize_session_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = SESSION_ID_PATTERN.sub("-", value.strip().lower())
    normalized = MULTI_DASH_PATTERN.sub("-", normalized).strip(".-_")
    if not normalized:
        return None
    if len(normalized) > MAX_SESSION_ID_LENGTH:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:SESSION_ID_HASH_LENGTH]
        keep = MAX_SESSION_ID_LENGTH - len(digest) - 1
        normalized = f"{normalized[:keep].rstrip('.-_')}-{digest}"
    if len(normalized) < 3:
        return None
    return normalized


def generate_session_id() -> str:
    stamp = utc_now_iso().replace("-", "").replace(":", "").replace("T", "-").replace("Z", "")
    token = secrets.token_hex(3)
    return f"sess-{stamp}-{token}"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceStateError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceStateError(f"{path} must contain a JSON object")
    return data


def load_session_id_sources(state_dir: Path = STATE_DIR) -> dict[str, Any]:
    data = _load_json(state_dir / DEFAULT_SESSION_SOURCES_FILENAME)
    if data is None:
        return {
            "schema_version": 1,
            "env_allowlist": [],
            "prefix_by_source": {},
            "allow_unconfigured_generic_env": False,
        }
    if data.get("schema_version") != 1:
        raise WorkspaceStateError(
            f"unsupported session-id-sources schema_version: {data.get('schema_version')!r}"
        )
    allowlist = data.get("env_allowlist", [])
    prefixes = data.get("prefix_by_source", {})
    if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
        raise WorkspaceStateError("session-id-sources.env_allowlist must be a list of strings")
    if not isinstance(prefixes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in prefixes.items()):
        raise WorkspaceStateError("session-id-sources.prefix_by_source must be a string map")
    return data


def load_current_session_binding(repo_root: Path) -> dict[str, Any] | None:
    return _load_json(repo_root / ".vaws-local" / CURRENT_SESSION_FILENAME)


def write_current_session_binding(
    repo_root: Path,
    *,
    session_id: str,
    source: str,
    session_file: Path | None = None,
    base_repo_root: Path | None = None,
) -> Path:
    normalized = normalize_session_id(session_id)
    if normalized is None:
        raise WorkspaceStateError(f"invalid session id: {session_id!r}")
    state_dir = repo_root / ".vaws-local"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / CURRENT_SESSION_FILENAME
    payload: dict[str, Any] = {
        "schema_version": 1,
        "session_id": normalized,
        "source": source,
        "created_at": utc_now_iso(),
    }
    if session_file is not None:
        payload["session_file"] = str(session_file.expanduser().resolve())
    if base_repo_root is not None:
        payload["base_repo_root"] = str(base_repo_root.expanduser().resolve())
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(state_dir)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
    return path


def git_current_branch(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "branch", "--show-current"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def derive_from_branch(branch: str | None) -> str | None:
    if not branch:
        return None
    for prefix in ("session/", "task/"):
        if branch.startswith(prefix):
            return normalize_session_id(branch[len(prefix) :])
    if branch.startswith("pr/"):
        tail = normalize_session_id(branch[len("pr/") :])
        return normalize_session_id(f"pr-{tail}") if tail else None
    return None


def _candidate_from_env(name: str, *, prefix: str | None = None) -> tuple[str, str] | None:
    value = os.environ.get(name)
    if not value:
        return None
    raw = f"{prefix}-{value}" if prefix else value
    return raw, f"env:{name}"


def resolve_session_id(
    *,
    explicit: str | None = None,
    repo_root: Path,
    persist_generated: bool = True,
    use_current_binding: bool = True,
) -> SessionId:
    candidates: list[tuple[str, str]] = []
    if explicit:
        candidates.append((explicit, "cli"))

    for env_name in ("VAWS_SESSION_ID", "VAWS_AGENT_SESSION_ID"):
        candidate = _candidate_from_env(env_name)
        if candidate is not None:
            candidates.append(candidate)

    cfg = load_session_id_sources(repo_root / ".vaws-local")
    prefixes = cfg.get("prefix_by_source", {})
    for env_name in cfg.get("env_allowlist", []):
        if env_name in {"VAWS_SESSION_ID", "VAWS_AGENT_SESSION_ID"}:
            continue
        candidate = _candidate_from_env(env_name, prefix=prefixes.get(env_name))
        if candidate is not None:
            candidates.append(candidate)

    if use_current_binding:
        current = load_current_session_binding(repo_root)
        if current and isinstance(current.get("session_id"), str):
            candidates.append((current["session_id"], "current-session"))

    derived = derive_from_branch(git_current_branch(repo_root))
    if derived:
        candidates.append((derived, "git-branch"))

    for raw, source in candidates:
        normalized = normalize_session_id(raw)
        if normalized:
            return SessionId(value=normalized, source=source)

    generated = generate_session_id()
    if persist_generated:
        write_current_session_binding(repo_root, session_id=generated, source="generated")
    return SessionId(value=generated, source="generated")
