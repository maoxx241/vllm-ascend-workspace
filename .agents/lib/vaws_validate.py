#!/usr/bin/env python3
"""Shared validation helpers for VAWS agent-facing scripts."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")


class ValidationError(ValueError):
    """Raised for deterministic user-input validation failures."""


def require_env_name(name: str) -> str:
    if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
        raise ValidationError(
            f"invalid env var name: {name!r}; use ASCII [A-Za-z_][A-Za-z0-9_]*"
        )
    return name


def require_safe_id(value: str, *, label: str = "id") -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(
            f"invalid {label}: use 3-64 chars from A-Z a-z 0-9 _ . -; "
            "no slashes, spaces, path traversal, or absolute paths"
        )
    return value


def require_remote_leaf(value: str, *, label: str = "id") -> str:
    safe = require_safe_id(value, label=label)
    path = PurePosixPath(safe)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise ValidationError(f"invalid {label}: must be one remote path segment")
    return safe


def ensure_child_path(root: Path, child: Path) -> Path:
    root_resolved = root.expanduser().resolve()
    child_resolved = child.expanduser().resolve()
    if root_resolved != child_resolved and root_resolved not in child_resolved.parents:
        raise ValidationError(f"path escapes state dir: {child_resolved}")
    return child_resolved


def parse_device_csv(value: str | None, *, label: str = "devices") -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty comma-separated device list")
    devices: list[int] = []
    seen: set[int] = set()
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            raise ValidationError(f"{label} contains an empty device id")
        try:
            device = int(token, 10)
        except ValueError as exc:
            raise ValidationError(f"{label} contains a non-integer device id: {token!r}") from exc
        if device < 0:
            raise ValidationError(f"{label} contains a negative device id: {device}")
        if device in seen:
            raise ValidationError(f"{label} contains a duplicate device id: {device}")
        seen.add(device)
        devices.append(device)
    return sorted(devices)
