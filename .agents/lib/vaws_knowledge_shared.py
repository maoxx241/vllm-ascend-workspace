#!/usr/bin/env python3
"""Trust boundary for the read-only shared knowledge cache.

Only ``corpus/verified/`` documents from a declared source may be mounted as
the shared layer. Review-zone labels (``layer: project`` / ``unverified``) and
trust-layer labels (``layer: shared``) are different things: a project-zone
document with ``status: verified`` is not shared corpus.

The same checks run at import and again when probing, querying, or fetching a
cached entry so a pre-existing bad cache cannot bypass the importer.
"""

from __future__ import annotations

import json
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import vaws_knowledge_v2 as v2

SHARED_CACHE_METADATA = "cache-metadata.json"
DEFAULT_SOURCE_REPO = "vllm-ascend-workspace/vaws-knowledge"
YAML_SUFFIX = ".yaml"
SOURCE_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
SOURCE_REF_RE = re.compile(r"^[0-9a-f]{40}$")

ABSENT = "absent"
AVAILABLE = "available"
UNAVAILABLE = "unavailable"
DEGRADED = "degraded"


def source_identity_problems(
    source_repo: Any,
    source_ref: Any,
    *,
    expect_repo: str | None = None,
    expect_ref: str | None = None,
) -> list[str]:
    """Return problems that prevent a verified-source guarantee."""

    problems: list[str] = []
    if not isinstance(source_repo, str) or not SOURCE_REPO_RE.fullmatch(source_repo):
        problems.append(
            "source_repo is missing or malformed; refusing a verified-source guarantee"
        )
    if not isinstance(source_ref, str) or not SOURCE_REF_RE.fullmatch(source_ref):
        problems.append(
            "source_ref is missing or malformed; refusing a verified-source guarantee"
        )
    if (
        expect_repo
        and isinstance(source_repo, str)
        and SOURCE_REPO_RE.fullmatch(source_repo)
        and source_repo != expect_repo
    ):
        problems.append(
            f"source_repo {source_repo!r} does not match configured {expect_repo!r}"
        )
    if (
        expect_ref
        and isinstance(source_ref, str)
        and SOURCE_REF_RE.fullmatch(source_ref)
        and source_ref != expect_ref
    ):
        problems.append(
            f"source_ref {source_ref!r} does not match configured {expect_ref!r}"
        )
    return problems


def iter_shared_yaml_files(root: Path) -> list[Path]:
    """Non-recursive ``*.yaml`` files under a corpus zone or cache directory."""

    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.name == SHARED_CACHE_METADATA:
            continue
        if path.name.endswith(YAML_SUFFIX):
            found.append(path)
    return found


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_file():
        path.chmod(0o644)
        path.unlink()
        return
    for child in path.iterdir():
        if child.is_file():
            child.chmod(0o644)
            child.unlink()
        else:
            _remove_tree(child)
    path.rmdir()


def load_cache_metadata(shared_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    metadata_path = shared_dir / SHARED_CACHE_METADATA
    if not shared_dir.is_dir() or not metadata_path.is_file():
        return None, []
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"shared cache metadata unreadable: {exc}"]
    if not isinstance(payload, dict):
        return None, ["shared cache metadata must be an object"]
    return payload, []


def validate_shared_document(
    document: Mapping[str, Any], *, path: str
) -> None:
    """Validate one document as eligible shared verified-zone corpus."""

    v2.validate_document(document, path=path, context=v2.VERIFIED_CONTEXT)


def load_shared_source_documents(source: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load and validate every ``*.yaml`` document under a declared source zone.

    Kind is taken from the validated document, not from the local v1/v2
    filename convention. Split files of one kind are kept as separate
    documents; colliding uuids fail the load.
    """

    staged: list[dict[str, Any]] = []
    problems: list[str] = []
    seen_uuids: dict[str, str] = {}
    for path in iter_shared_yaml_files(source):
        try:
            document = v2.load_document(path)
            validate_shared_document(document, path=str(path))
        except v2.KnowledgeV2Error as exc:
            problems.append(str(exc))
            continue
        kind = document.get("kind")
        if not isinstance(kind, str) or not v2.KIND_RE.fullmatch(kind):
            problems.append(f"{path}: document.kind is missing or malformed")
            continue
        entries = document.get("entries", [])
        if not isinstance(entries, list):
            problems.append(f"{path}: entries must be an array")
            continue
        collision = False
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                problems.append(f"{path}: entries[{index}] is not an object")
                collision = True
                continue
            entry_uuid = entry.get("uuid")
            if isinstance(entry_uuid, str):
                previous = seen_uuids.get(entry_uuid)
                if previous:
                    problems.append(
                        f"{path}: uuid {entry_uuid} collides with {previous}"
                    )
                    collision = True
                else:
                    seen_uuids[entry_uuid] = f"{path.name}#entries[{index}]"
        if collision:
            continue
        staged.append(
            {
                "kind": kind,
                "path": path,
                "name": path.name,
                "document": document,
                "entries": len(entries),
            }
        )
    return staged, problems


def install_shared_cache(
    shared_dir: Path,
    staged: list[dict[str, Any]],
    metadata: Mapping[str, Any],
) -> None:
    """Replace ``shared_dir`` only after the new snapshot is fully written."""

    parent = shared_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{shared_dir.name}.staging"
    backup = parent / f".{shared_dir.name}.backup"
    _remove_tree(staging)
    _remove_tree(backup)
    staging.mkdir(parents=True)
    imported: list[str] = []
    try:
        for item in staged:
            target = staging / item["name"]
            shutil.copyfile(item["path"], target)
            target.chmod(0o444)
            imported.append(target.name)
        payload = dict(metadata)
        payload["documents"] = imported
        (staging / SHARED_CACHE_METADATA).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if shared_dir.exists():
            shared_dir.rename(backup)
        staging.rename(shared_dir)
    except Exception:
        if backup.exists() and not shared_dir.exists():
            backup.rename(shared_dir)
        raise
    finally:
        _remove_tree(staging)
        _remove_tree(backup)


def inspect_shared_cache(
    shared_dir: Path,
    *,
    expect_repo: str | None = None,
    expect_ref: str | None = None,
) -> dict[str, Any]:
    """Probe a cache directory without treating untrusted files as shared."""

    metadata_path = shared_dir / SHARED_CACHE_METADATA
    if not shared_dir.is_dir() or not metadata_path.is_file():
        return {
            "status": ABSENT,
            "path": str(shared_dir),
            "detail": "no shared cache pulled from vaws-knowledge yet",
            "remedy": (
                "python3 .agents/scripts/knowledge_shared_cache.py import "
                "--from <clone>/corpus/verified "
                f"--source-repo {DEFAULT_SOURCE_REPO} --source-ref <commit-sha>"
            ),
            "problems": [],
            "entries": [],
            "documents": [],
        }

    metadata, meta_problems = load_cache_metadata(shared_dir)
    if metadata is None:
        return {
            "status": UNAVAILABLE,
            "path": str(shared_dir),
            "detail": meta_problems[0] if meta_problems else "shared cache metadata unreadable",
            "problems": meta_problems,
            "entries": [],
            "documents": [],
            "source_repo": None,
            "source_ref": None,
        }

    source_repo = metadata.get("source_repo")
    source_ref = metadata.get("source_ref")
    identity_problems = source_identity_problems(
        source_repo, source_ref, expect_repo=expect_repo, expect_ref=expect_ref
    )
    staged, document_problems = load_shared_source_documents(shared_dir)
    yaml_files = [path.name for path in iter_shared_yaml_files(shared_dir)]
    problems = identity_problems + document_problems
    if identity_problems:
        return {
            "status": UNAVAILABLE,
            "path": str(shared_dir),
            "detail": identity_problems[0],
            "problems": problems,
            "entries": [],
            "documents": yaml_files,
            "source_repo": source_repo if isinstance(source_repo, str) else None,
            "source_ref": source_ref if isinstance(source_ref, str) else None,
            "pulled_at": metadata.get("pulled_at"),
            "metadata": metadata,
        }
    if document_problems or len(staged) != len(yaml_files):
        detail = (
            document_problems[0]
            if document_problems
            else "shared cache documents are not eligible verified-zone corpus"
        )
        return {
            "status": UNAVAILABLE,
            "path": str(shared_dir),
            "detail": detail,
            "problems": problems,
            "entries": [],
            "documents": yaml_files,
            "source_repo": source_repo,
            "source_ref": source_ref,
            "pulled_at": metadata.get("pulled_at"),
            "metadata": metadata,
        }

    entries: list[dict[str, Any]] = []
    for item in staged:
        for entry in item["document"].get("entries", []):
            record = deepcopy(dict(entry))
            record["_kind"] = item["kind"]
            record["_source_file"] = item["name"]
            record["_source_repo"] = source_repo
            record["_source_ref"] = source_ref
            entries.append(record)
    return {
        "status": AVAILABLE,
        "path": str(shared_dir),
        "detail": "shared layer is mounted read-only",
        "problems": [],
        "entries": entries,
        "documents": [item["name"] for item in staged],
        "source_repo": source_repo,
        "source_ref": source_ref,
        "pulled_at": metadata.get("pulled_at"),
        "metadata": metadata,
        "entry_count": len(entries),
    }


def load_shared_entries(
    shared_dir: Path,
    *,
    expect_repo: str | None = None,
    expect_ref: str | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    inspection = inspect_shared_cache(
        shared_dir, expect_repo=expect_repo, expect_ref=expect_ref
    )
    if inspection["status"] != AVAILABLE:
        return [], list(inspection.get("problems") or []), inspection
    return list(inspection["entries"]), [], inspection
