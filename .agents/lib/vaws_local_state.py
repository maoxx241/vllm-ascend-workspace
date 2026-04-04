#!/usr/bin/env python3
"""Local untracked state helpers for vllm-ascend-workspace.

This module centralizes repo-local runtime state that should never be tracked.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIRNAME = ".vaws-local"
PROFILE_FILENAME = "machine-profile.json"
INVENTORY_FILENAME = "machine-inventory.json"
LEGACY_INVENTORY_FILENAME = ".machine-inventory.json"
PROFILE_SCHEMA_VERSION = 1
CONTAINER_PREFIX = "vaws-"
USERNAME_PATTERN = re.compile(r"^[a-z0-9]{3,32}$")
RANDOM_ALPHABET = string.ascii_lowercase + string.digits

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / STATE_DIRNAME
PROFILE_PATH = STATE_DIR / PROFILE_FILENAME
INVENTORY_PATH = STATE_DIR / INVENTORY_FILENAME
LEGACY_INVENTORY_PATH = ROOT / LEGACY_INVENTORY_FILENAME


class WorkspaceStateError(RuntimeError):
    """Raised for deterministic user-facing local-state failures."""


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def ensure_state_dir(path: Path = STATE_DIR) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def normalize_machine_username(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise WorkspaceStateError("machine username must be non-empty")
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise WorkspaceStateError(
            "machine username must be 3-32 characters of English letters and digits only"
        )
    return normalized


validate_machine_username = normalize_machine_username


def generate_machine_username(
    *,
    prefix: str = "agent",
    suffix_length: int = 6,
    existing: set[str] | None = None,
) -> str:
    cleaned_prefix = "".join(ch for ch in prefix.lower() if ch.isalnum()) or "agent"
    cleaned_prefix = cleaned_prefix[: max(1, 32 - suffix_length)]
    existing = existing or set()
    for _ in range(128):
        suffix = "".join(secrets.choice(RANDOM_ALPHABET) for _ in range(suffix_length))
        candidate = f"{cleaned_prefix}{suffix}"
        if candidate not in existing and USERNAME_PATTERN.fullmatch(candidate):
            return candidate
    raise WorkspaceStateError("unable to generate a unique machine username")


def default_container_name(machine_username: str) -> str:
    return f"{CONTAINER_PREFIX}{normalize_machine_username(machine_username)}"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceStateError(f"invalid JSON in {path}: {exc}") from exc


def _save_json(path: Path, data: Any) -> None:
    ensure_state_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _validate_profile(profile: Any, *, where: str = "profile") -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise WorkspaceStateError(f"{where} must be an object")
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise WorkspaceStateError(
            f"unsupported profile schema_version in {where}: {profile.get('schema_version')!r}"
        )
    machine_username = normalize_machine_username(str(profile.get("machine_username", "")))
    container_name = profile.get("container_name")
    expected_container_name = default_container_name(machine_username)
    if container_name is None:
        container_name = expected_container_name
    if not isinstance(container_name, str) or container_name != expected_container_name:
        raise WorkspaceStateError(
            f"{where}.container_name must be {expected_container_name!r}"
        )
    source = profile.get("source")
    if source is not None and source not in {"user", "generated"}:
        raise WorkspaceStateError(f"{where}.source must be 'user' or 'generated' when present")
    for field in ("created_at", "updated_at"):
        value = profile.get(field)
        if value is not None and not isinstance(value, str):
            raise WorkspaceStateError(f"{where}.{field} must be a string when present")
    normalized: dict[str, Any] = dict(profile)
    normalized["machine_username"] = machine_username
    normalized["container_name"] = container_name
    return normalized


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any] | None:
    path = path.expanduser().resolve()
    if not path.exists():
        return None
    data = _load_json(path)
    return _validate_profile(data, where=str(path))


def save_profile(profile: dict[str, Any], path: Path = PROFILE_PATH) -> dict[str, Any]:
    path = path.expanduser().resolve()
    normalized = _validate_profile(profile, where="profile")
    _save_json(path, normalized)
    return normalized


def ensure_profile(
    *,
    path: Path = PROFILE_PATH,
    machine_username: str | None = None,
    allow_update: bool = False,
) -> tuple[dict[str, Any], str]:
    path = path.expanduser().resolve()
    existing = load_profile(path)
    if existing is not None:
        if machine_username is None:
            return existing, "existing"
        normalized_name = normalize_machine_username(machine_username)
        if existing["machine_username"] == normalized_name:
            return existing, "existing"
        if not allow_update:
            raise WorkspaceStateError(
                "machine profile already exists; rerun with --allow-update to change it"
            )
        updated = dict(existing)
        updated["machine_username"] = normalized_name
        updated["container_name"] = default_container_name(normalized_name)
        updated["source"] = "user"
        updated["updated_at"] = utc_now_iso()
        return save_profile(updated, path=path), "updated"

    existing_names: set[str] = set()
    normalized_name = (
        normalize_machine_username(machine_username)
        if machine_username is not None
        else generate_machine_username(existing=existing_names)
    )
    now = utc_now_iso()
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "machine_username": normalized_name,
        "container_name": default_container_name(normalized_name),
        "source": "user" if machine_username is not None else "generated",
        "created_at": now,
        "updated_at": now,
    }
    return save_profile(profile, path=path), "created"


def profile_summary(path: Path = PROFILE_PATH) -> dict[str, Any]:
    path = path.expanduser().resolve()
    summary: dict[str, Any] = {
        "state_dir": str(path.parent),
        "profile_path": str(path),
        "inventory_path": str(INVENTORY_PATH),
        "legacy_inventory_path": str(LEGACY_INVENTORY_PATH),
        "exists": path.exists(),
        "machine_username": None,
        "container_name": None,
        "source": None,
        "created_at": None,
        "updated_at": None,
    }
    if not path.exists():
        return summary
    profile = load_profile(path)
    if profile is None:
        return summary
    summary.update(
        {
            "machine_username": profile["machine_username"],
            "container_name": profile["container_name"],
            "source": profile.get("source"),
            "created_at": profile.get("created_at"),
            "updated_at": profile.get("updated_at"),
        }
    )
    return summary


def resolve_inventory_read_path(preferred_path: Path = INVENTORY_PATH) -> Path:
    preferred_path = preferred_path.expanduser().resolve()
    if same_path(preferred_path, INVENTORY_PATH) and not preferred_path.exists() and LEGACY_INVENTORY_PATH.exists():
        return LEGACY_INVENTORY_PATH.expanduser().resolve()
    return preferred_path
