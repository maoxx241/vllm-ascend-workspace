#!/usr/bin/env python3
"""Shared Run Manifest v1 helpers for workspace domain workflows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

SCHEMA_VERSION = 1
RUN_TYPES = frozenset(
    {
        "change-validation",
        "correctness",
        "debug",
        "performance",
        "profiling",
    }
)
RUN_STATUSES = frozenset(
    {"planned", "running", "passed", "failed", "inconclusive", "cancelled"}
)
TERMINAL_STATUSES = frozenset({"passed", "failed", "inconclusive", "cancelled"})
STATUS_TRANSITIONS = {
    "planned": frozenset({"running", "cancelled"}),
    "running": frozenset({"passed", "failed", "inconclusive", "cancelled"}),
    "passed": frozenset(),
    "failed": frozenset(),
    "inconclusive": frozenset(),
    "cancelled": frozenset(),
}
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|ACCESS_?KEY|AUTH|CREDENTIAL|PASS(?:WORD)?|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)


class RunManifestError(ValueError):
    """Raised when a manifest violates the v1 contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def generate_run_id(run_type: str, *, now: str | None = None) -> str:
    if run_type not in RUN_TYPES:
        raise RunManifestError(f"unsupported run_type: {run_type!r}")
    timestamp = (now or utc_now()).replace("-", "").replace(":", "")
    timestamp = timestamp.replace("T", "-").replace("Z", "").split(".", 1)[0]
    return f"{run_type}-{timestamp}-{uuid.uuid4().hex[:8]}"


def new_manifest(
    *,
    run_type: str,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    workspace_snapshot: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
    model: Mapping[str, Any] | None = None,
    topology: Mapping[str, Any] | None = None,
    command: Sequence[str] | None = None,
    environment_variables: Mapping[str, str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    timestamp = created_at or utc_now()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or generate_run_id(run_type, now=timestamp),
        "parent_run_id": parent_run_id,
        "run_type": run_type,
        "workspace_snapshot": dict(workspace_snapshot or {}),
        "environment": dict(environment or {}),
        "model": dict(model or {}),
        "topology": dict(topology or {}),
        "command": list(command or []),
        "environment_variables": dict(environment_variables or {}),
        "artifacts": [],
        "status": "planned",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    validate_manifest(manifest)
    return manifest


def _require_mapping(value: Any, path: str, errors: list[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return {}
    return value


def _validate_safe_id(value: Any, path: str, errors: list[str], *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        errors.append(f"{path} must match {SAFE_ID_RE.pattern}")


def _validate_artifacts(value: Any, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append("artifacts must be an array")
        return
    names: set[str] = set()
    for index, artifact in enumerate(value):
        item = _require_mapping(artifact, f"artifacts[{index}]", errors)
        for field in ("name", "kind", "uri"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f"artifacts[{index}].{field} must be a non-empty string")
        name = item.get("name")
        if isinstance(name, str):
            if name in names:
                errors.append(f"artifact name is duplicated: {name!r}")
            names.add(name)
        sha256 = item.get("sha256")
        if sha256 is not None and (
            not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256)
        ):
            errors.append(f"artifacts[{index}].sha256 must be 64 lowercase hex characters")


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    _validate_safe_id(manifest.get("run_id"), "run_id", errors)
    _validate_safe_id(manifest.get("parent_run_id"), "parent_run_id", errors, nullable=True)

    run_type = manifest.get("run_type")
    if run_type not in RUN_TYPES:
        errors.append(f"run_type must be one of: {', '.join(sorted(RUN_TYPES))}")
    status = manifest.get("status")
    if status not in RUN_STATUSES:
        errors.append(f"status must be one of: {', '.join(sorted(RUN_STATUSES))}")

    for field in ("workspace_snapshot", "environment", "model", "topology"):
        _require_mapping(manifest.get(field), field, errors)

    command = manifest.get("command")
    if not isinstance(command, list) or any(not isinstance(part, str) for part in command):
        errors.append("command must be an array of strings")

    env = _require_mapping(manifest.get("environment_variables"), "environment_variables", errors)
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            errors.append("environment_variables keys and values must be strings")
            continue
        if SECRET_ENV_RE.search(key):
            errors.append(f"environment_variables must not contain secret-like key: {key}")

    _validate_artifacts(manifest.get("artifacts"), errors)

    for field in ("created_at", "updated_at"):
        timestamp = manifest.get(field)
        if not isinstance(timestamp, str) or not RFC3339_UTC_RE.fullmatch(timestamp):
            errors.append(f"{field} must be an RFC3339 UTC timestamp ending in Z")

    allowed = {
        "schema_version",
        "run_id",
        "parent_run_id",
        "run_type",
        "workspace_snapshot",
        "environment",
        "model",
        "topology",
        "command",
        "environment_variables",
        "artifacts",
        "status",
        "created_at",
        "updated_at",
    }
    unknown = sorted(set(manifest) - allowed)
    if unknown:
        errors.append(f"unknown top-level fields: {', '.join(unknown)}")
    missing = sorted(allowed - set(manifest))
    if missing:
        errors.append(f"missing top-level fields: {', '.join(missing)}")

    if errors:
        raise RunManifestError("; ".join(errors))


def transition_status(
    manifest: Mapping[str, Any], status: str, *, updated_at: str | None = None
) -> dict[str, Any]:
    validate_manifest(manifest)
    current = manifest["status"]
    if status not in STATUS_TRANSITIONS[current]:
        raise RunManifestError(f"invalid status transition: {current} -> {status}")
    updated = deepcopy(dict(manifest))
    updated["status"] = status
    updated["updated_at"] = updated_at or utc_now()
    validate_manifest(updated)
    return updated


def add_artifact(
    manifest: Mapping[str, Any],
    *,
    name: str,
    kind: str,
    uri: str,
    sha256: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    validate_manifest(manifest)
    artifact = {"name": name, "kind": kind, "uri": uri}
    if sha256 is not None:
        artifact["sha256"] = sha256
    updated = deepcopy(dict(manifest))
    updated["artifacts"].append(artifact)
    updated["updated_at"] = updated_at or utc_now()
    validate_manifest(updated)
    return updated


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunManifestError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(payload, MutableMapping):
        raise RunManifestError("manifest root must be an object")
    validate_manifest(payload)
    return dict(payload)


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    validate_manifest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
