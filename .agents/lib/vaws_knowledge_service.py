"""Scaffold ServiceConfig: packaged shared + this repo's project/candidate."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import hashlib
from datetime import datetime, timezone

from vaws_knowledge.redact import REDACTION_PROFILE
from vaws_knowledge.server.layers import (
    DEFAULT_STATUSES,
    OPT_IN_STATUSES,
    ServiceConfig,
    load_config,
    load_entries,
)
from vaws_knowledge.server.query import SCOPE_DIMENSIONS, query as commons_query

COORDINATE_DIMENSIONS = SCOPE_DIMENSIONS
COORDINATE_UNKNOWN = "unknown"


class KnowledgeError(ValueError):
    """Scaffold-facing knowledge error (capture, query, or hook)."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def knowledge_session_key(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise KnowledgeError("session id must be a non-empty string")
    return hashlib.sha256(session_id.strip().encode("utf-8")).hexdigest()[:20]


def normalize_coordinate(
    coordinate: Mapping[str, Any] | None, *, source: str = "unavailable"
) -> dict[str, str]:
    del source
    out: dict[str, str] = {}
    incoming = coordinate if isinstance(coordinate, Mapping) else {}
    for name in COORDINATE_DIMENSIONS:
        value = incoming.get(name)
        text = str(value).strip() if value is not None else ""
        out[name] = text or COORDINATE_UNKNOWN
    return out


def unknown_coordinate_dimensions(coordinate: Mapping[str, Any]) -> list[str]:
    return [
        name
        for name in COORDINATE_DIMENSIONS
        if str(coordinate.get(name) or COORDINATE_UNKNOWN).strip() == COORDINATE_UNKNOWN
    ]


def query_knowledge(
    *,
    knowledge_dir: Path,
    query: str,
    kinds: list[str] | None = None,
    bodies: list[str] | None = None,
    limit: int = 3,
    include_deprecated: bool = False,
    min_score: int = 0,
    include_unverified: bool = True,
) -> list[dict[str, Any]]:
    """Project-layer query through the installed engine, shaped for existing callers.

    Each record includes the entry ``status`` and the layer it was read from
    so a caller can tell verified/unverified/deprecated and shared/project
    apart. ``include_unverified`` defaults to True because every project-layer
    entry is unverified. ``include_deprecated`` is forwarded as an explicit
    status list; the package never adds deprecated on its own.
    """

    statuses = None
    if include_deprecated:
        statuses = list(DEFAULT_STATUSES)
        if include_unverified:
            statuses.extend(
                status for status in OPT_IN_STATUSES if status not in statuses
            )
        if "deprecated" not in statuses:
            statuses.append("deprecated")
    repo = infer_repo_root(knowledge_dir, knowledge_dir.parent)
    config = service_config(repo, project_root=knowledge_dir)
    selected = list(kinds) if kinds else [None]
    matches: list[dict[str, Any]] = []
    for kind in selected:
        response = commons_query(
            config,
            text=query,
            kind=kind,
            bodies=bodies,
            limit=max(limit, 1),
            include_unverified=include_unverified,
            statuses=statuses,
        )
        for result in response.results:
            payload = result.to_dict()
            score = float((payload.get("match") or {}).get("score") or result.score or 0)
            if min_score and score * 10 < min_score:
                # v1 min_score was integer token overlap; package scores are smaller floats.
                if score <= 0:
                    continue
            matches.append(
                {
                    "id": payload.get("slug"),
                    "uuid": payload.get("uuid"),
                    "kind": payload.get("kind"),
                    "status": payload.get("status"),
                    "body": payload.get("body") or "rule",
                    "summary": payload.get("summary"),
                    "score": score,
                    "source_file": (payload.get("source") or {}).get("file")
                    if isinstance(payload.get("source"), Mapping)
                    else payload.get("kind"),
                    "layer": payload.get("layer") or "project",
                    "schema_version": 2,
                    "resolution": payload.get("resolution") or "",
                }
            )
    matches.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("kind")), str(item.get("id"))))
    return matches[:limit]


def get_knowledge_entry(*, knowledge_dir: Path, entry_id: str) -> dict[str, Any] | None:
    import vaws_knowledge_v2 as v2

    entries, _problems = v2.load_entries(knowledge_dir, validate=False)
    for entry in entries:
        if entry_id in {entry.get("slug"), entry.get("uuid")}:
            record = {key: value for key, value in entry.items() if not str(key).startswith("_")}
            return {
                "kind": entry.get("_kind"),
                "source_file": entry.get("_source_file"),
                "entry": record,
                "layer": "project",
                "schema_version": 2,
            }
    return None


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeError(f"{path} must use JSON-compatible YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise KnowledgeError(f"{path}: document root must be an object")
    return payload

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")

SOURCE_REPO = "vllm-ascend-workspace/vaws-knowledge"
PROJECT_ROOT_RELATIVE = ".agents/knowledge"
CANDIDATE_ROOT_RELATIVE = ".vaws-local/knowledge/candidate"
SHARED_AVAILABLE = "available"
SHARED_ABSENT = "absent"
SHARED_REMEDY = "uv sync"


def infer_repo_root(knowledge_dir: Path, fallback: Path) -> Path:
    resolved = knowledge_dir.resolve()
    if resolved.parent.name == ".agents":
        return resolved.parent.parent
    return fallback


def origin_repo_from_url(url: str) -> str:
    text = url.strip()
    text = re.sub(r"\.git$", "", text)
    if text.startswith("git@") and ":" in text:
        return text.split(":", 1)[1]
    parts = [part for part in re.split(r"[/:]", text) if part]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return text or "local/unpublished"


def origin_repo_from_git(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return "local/unpublished"
    return origin_repo_from_url(proc.stdout.strip())


def knowledge_identity(repo_root: Path) -> dict[str, str]:
    return {
        "contributor": "anonymous",
        "origin_repo": origin_repo_from_git(repo_root),
        "redaction_profile": REDACTION_PROFILE,
    }


def knowledge_server_env(repo_root: Path) -> dict[str, str]:
    """Relative env paths, matching tracked mcp.json style."""

    identity = knowledge_identity(repo_root)
    return {
        "VAWS_KNOWLEDGE_PROJECT_ROOTS": PROJECT_ROOT_RELATIVE,
        "VAWS_KNOWLEDGE_CANDIDATE_ROOT": CANDIDATE_ROOT_RELATIVE,
        "VAWS_KNOWLEDGE_ORIGIN_REPO": identity["origin_repo"],
        "VAWS_KNOWLEDGE_REDACTION_PROFILE": identity["redaction_profile"],
    }


def probe_shared() -> dict[str, Any]:
    try:
        from vaws_knowledge import corpus as packaged
    except ImportError:
        return {
            "status": SHARED_ABSENT,
            "path": None,
            "detail": "vaws-knowledge is not installed",
            "remedy": SHARED_REMEDY,
            "problems": [],
            "documents": [],
            "source_repo": SOURCE_REPO,
            "source_ref": None,
        }
    root = packaged.corpus_root()
    source_ref = packaged.installed_commit()
    if not root.is_dir():
        return {
            "status": SHARED_ABSENT,
            "path": str(root),
            "detail": "installed vaws-knowledge has no corpus",
            "remedy": SHARED_REMEDY,
            "problems": [],
            "documents": [],
            "source_repo": SOURCE_REPO,
            "source_ref": source_ref,
        }
    files = list(packaged.iter_entry_files())
    return {
        "status": SHARED_AVAILABLE,
        "path": str(root),
        "detail": "shared layer is the installed vaws-knowledge corpus",
        "problems": [],
        "documents": [path.name for path in files],
        "source_repo": SOURCE_REPO,
        "source_ref": source_ref,
    }


def service_config(
    repo_root: Path,
    *,
    project_root: Path | None = None,
    candidate_root: Path | None = None,
) -> ServiceConfig:
    project = project_root or (repo_root / PROJECT_ROOT_RELATIVE)
    candidate = candidate_root or (repo_root / CANDIDATE_ROOT_RELATIVE)
    return load_config(
        {
            "layers": {
                "project": {"roots": [str(project)]},
                "candidate": {"root": str(candidate)},
            },
            "identity": knowledge_identity(repo_root),
        },
        env={},
        base_dir=repo_root,
    )


def scope_from_coordinate(coordinate: Mapping[str, str]) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    for name in COORDINATE_DIMENSIONS:
        value = str(coordinate.get(name) or COORDINATE_UNKNOWN).strip() or COORDINATE_UNKNOWN
        scope[name] = {"values": [value]}
    return scope


def slugify(text: str) -> str:
    slug = _SLUG_UNSAFE.sub("-", text.strip().lower()).strip("-")
    return slug[:80] or "captured-entry"


def _payload_rule(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = payload.get("rule")
    if isinstance(nested, Mapping):
        return dict(nested)
    return {}


def commons_entry(payload: Mapping[str, Any], coordinate: Mapping[str, str]) -> dict[str, Any]:
    nested = _payload_rule(payload)
    slug = str(
        payload.get("entry_id")
        or payload.get("slug")
        or nested.get("slug")
        or slugify(str(payload.get("summary") or nested.get("summary") or "captured-entry"))
    )
    rule = {
        "summary": str(payload.get("summary") or nested.get("summary") or slug),
        "symptom": str(
            payload.get("symptom") or nested.get("symptom") or payload.get("summary") or slug
        ),
        "root_cause": str(
            payload.get("root_cause")
            or nested.get("root_cause")
            or "recorded at capture; mechanism not yet refined"
        ),
        "resolution": str(
            payload.get("resolution") or nested.get("resolution") or "recorded at capture"
        ),
    }
    avoidance = payload.get("avoidance") or nested.get("avoidance")
    if avoidance:
        rule["avoidance"] = str(avoidance)
    fingerprints = payload.get("fingerprints")
    if fingerprints is None:
        fingerprints = nested.get("fingerprints")
    if isinstance(fingerprints, list):
        rule["fingerprints"] = [str(item) for item in fingerprints if str(item).strip()]
    return {
        "slug": slug,
        "kind": str(payload.get("kind") or "known-failure-signatures"),
        "rule": rule,
        "scope": scope_from_coordinate(coordinate),
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def list_candidate_entries(
    repo_root: Path,
    *,
    project_root: Path | None = None,
    candidate_root: Path | None = None,
) -> list[dict[str, Any]]:
    config = service_config(
        repo_root, project_root=project_root, candidate_root=candidate_root
    )
    report = load_entries(config, ["candidate"])
    results: list[dict[str, Any]] = []
    for loaded in report.entries:
        entry = loaded.entry
        rule = entry.get("rule") if isinstance(entry.get("rule"), Mapping) else {}
        slug = str(entry.get("slug") or loaded.uuid)
        results.append(
            {
                "candidate_id": slug,
                "uuid": loaded.uuid,
                "slug": slug,
                "content_hash": entry.get("content_hash"),
                "kind": loaded.kind,
                "summary": rule.get("summary") or slug,
                "status": entry.get("status"),
                "confidence": entry.get("confidence"),
                "updated_at": (entry.get("lifecycle") or {}).get("updated_at"),
                "source": loaded.source,
            }
        )
    results.sort(key=lambda item: str(item["slug"]))
    return results


def find_candidate_entry(
    identifier: str,
    repo_root: Path,
    *,
    project_root: Path | None = None,
    candidate_root: Path | None = None,
) -> Any:
    config = service_config(
        repo_root, project_root=project_root, candidate_root=candidate_root
    )
    report = load_entries(config, ["candidate"])
    for loaded in report.entries:
        entry = loaded.entry
        markers = {
            loaded.uuid,
            str(entry.get("uuid") or ""),
            str(entry.get("slug") or ""),
            str(entry.get("content_hash") or ""),
        }
        if identifier in markers:
            return loaded
    raise KeyError(f"candidate not found: {identifier}")


def remove_candidate_entry(
    loaded: Any,
    *,
    reviewed_dir: Path,
    disposition: str,
    entry_id: str | None,
    reason: str | None,
    now: str,
) -> Path:
    """Archive one yaml entry and drop it from the candidate layer."""

    import yaml

    slug = str(loaded.entry.get("slug") or loaded.uuid)
    archive_path = reviewed_dir / f"{slug}.json"
    _write_json_atomic(
        archive_path,
        {
            "schema_version": 2,
            "candidate_id": slug,
            "uuid": loaded.uuid,
            "content_hash": loaded.entry.get("content_hash"),
            "disposition": disposition,
            "entry_id": entry_id,
            "reason": reason,
            "reviewed_at": now,
            "candidate": deepcopy(dict(loaded.entry)),
        },
    )
    path = Path(loaded.root) / loaded.source
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = [
        item
        for item in (document.get("entries") or [])
        if isinstance(item, Mapping) and item.get("uuid") != loaded.uuid
    ]
    document["entries"] = entries
    if entries:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    else:
        path.unlink(missing_ok=True)
    return archive_path


def already_promoted_slug(
    payload: Mapping[str, Any],
    knowledge_dir: Path,
) -> str | None:
    """Return the project slug if this capture is already a formal entry."""

    import vaws_knowledge_v2 as v2

    slug = str(commons_entry(payload, payload.get("environment") or {}).get("slug") or "")
    for path, _kind in v2.iter_documents(knowledge_dir):
        try:
            document = v2.load_document(path)
        except Exception:  # noqa: BLE001 - a broken project file is not a hit
            continue
        for entry in document.get("entries") or []:
            if not isinstance(entry, Mapping) or entry.get("status") == "deprecated":
                continue
            if slug and entry.get("slug") == slug:
                return slug
    return None
