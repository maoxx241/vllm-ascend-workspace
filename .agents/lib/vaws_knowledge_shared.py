#!/usr/bin/env python3
"""Trust boundary for the read-only shared knowledge cache.

Only ``corpus/verified/`` documents from a declared source may be mounted as
the shared layer. Review-zone labels (``layer: project`` / ``unverified``) and
trust-layer labels (``layer: shared``) are different things: a project-zone
document with ``status: verified`` is not shared corpus.

The same checks run at import and again when probing, querying, or fetching a
cached entry so a pre-existing bad cache cannot bypass the importer.

Source policy lives in ``source-policy.json``, written only by a successful
import from explicit ``--source-repo`` / ``--source-ref`` (and optional
``--expect-*``). Cache metadata cannot create or relax it. Query, get, status
and probe apply the policy. ``clear`` deletes it with the cache. Staging,
policy write and rename are one snapshot: a failed refresh keeps the previous
cache and policy; backup is removed only after the new directory is live.
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
SHARED_SOURCE_POLICY = "source-policy.json"
DEFAULT_SOURCE_REPO = "vllm-ascend-workspace/vaws-knowledge"
YAML_SUFFIX = ".yaml"
SOURCE_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
SOURCE_REF_RE = re.compile(r"^[0-9a-f]{40}$")
POLICY_SCHEMA_VERSION = 1

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
        if path.name in {SHARED_CACHE_METADATA, SHARED_SOURCE_POLICY}:
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


def load_source_policy(shared_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load importer-owned source expectations. Cache metadata cannot create this."""

    path = shared_dir / SHARED_SOURCE_POLICY
    if not path.is_file():
        return None, [
            "required source policy is missing; refusing to trust cache self-declaration"
        ]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"source policy unreadable: {exc}"]
    if not isinstance(payload, dict):
        return None, ["source policy must be an object"]
    if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        return None, ["source policy schema_version is missing or unsupported"]
    expect_repo = payload.get("expect_source_repo")
    expect_ref = payload.get("expect_source_ref")
    identity = source_identity_problems(expect_repo, expect_ref)
    if identity:
        return None, ["source policy is invalid: " + identity[0]]
    return payload, []


def build_source_policy(*, expect_repo: str, expect_ref: str) -> dict[str, Any]:
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "expect_source_repo": expect_repo,
        "expect_source_ref": expect_ref,
        "bound_by": "knowledge_shared_cache.import",
    }


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
    policy: Mapping[str, Any],
) -> None:
    """Replace cache documents, metadata, and source policy as one snapshot.

    The importer owns the policy file. Cached YAML/metadata cannot create or
    relax it. Staging is discarded on failure. The previous directory is kept
    as a backup until the new directory is live; backup is removed only after
    the live cache exists. ``clear`` deletes the policy with the cache.
    """

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
        (staging / SHARED_SOURCE_POLICY).write_text(
            json.dumps(dict(policy), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (staging / SHARED_SOURCE_POLICY).chmod(0o444)
        if shared_dir.exists():
            shared_dir.rename(backup)
        staging.rename(shared_dir)
    except Exception:
        if backup.exists() and not shared_dir.exists():
            backup.rename(shared_dir)
        raise
    finally:
        _remove_tree(staging)
        if shared_dir.exists():
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
            "expected_source_repo": None,
            "expected_source_ref": None,
        }

    policy, policy_problems = load_source_policy(shared_dir)
    source_repo = metadata.get("source_repo")
    source_ref = metadata.get("source_ref")
    policy_repo = policy.get("expect_source_repo") if policy else None
    policy_ref = policy.get("expect_source_ref") if policy else None
    bound_repo = expect_repo if expect_repo is not None else policy_repo
    bound_ref = expect_ref if expect_ref is not None else policy_ref
    identity_problems = list(policy_problems)
    identity_problems.extend(
        source_identity_problems(
            source_repo, source_ref, expect_repo=bound_repo, expect_ref=bound_ref
        )
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
            "expected_source_repo": policy_repo,
            "expected_source_ref": policy_ref,
            "pulled_at": metadata.get("pulled_at"),
            "metadata": metadata,
            "policy": policy,
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
            "expected_source_repo": policy_repo,
            "expected_source_ref": policy_ref,
            "pulled_at": metadata.get("pulled_at"),
            "metadata": metadata,
            "policy": policy,
        }

    entries: list[dict[str, Any]] = []
    for item in staged:
        for entry in item["document"].get("entries", []):
            record = deepcopy(dict(entry))
            record["_kind"] = item["kind"]
            record["_source_file"] = item["name"]
            record["_source_repo"] = source_repo
            record["_source_ref"] = source_ref
            record["_expected_source_repo"] = policy_repo
            record["_expected_source_ref"] = policy_ref
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
        "expected_source_repo": policy_repo,
        "expected_source_ref": policy_ref,
        "pulled_at": metadata.get("pulled_at"),
        "metadata": metadata,
        "policy": policy,
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
