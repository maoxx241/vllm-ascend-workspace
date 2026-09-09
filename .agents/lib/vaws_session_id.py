"""Canonicalize local task/report filenames. Not a task-identity resolver."""

from __future__ import annotations

import hashlib
import re

SESSION_ID_PATTERN = re.compile(r"[^a-z0-9._-]+")
MULTI_DASH_PATTERN = re.compile(r"-+")
MAX_SESSION_ID_LENGTH = 64
SESSION_ID_HASH_LENGTH = 8


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
