#!/usr/bin/env python3
"""Review and curate verified workspace knowledge candidates.

Two formal generations are curated here. ``promote``/``merge`` default to the
federated v2 contract (``<kind>.v2.yaml``) because new writes go forward;
``--schema 1`` keeps the legacy v1 envelope available for an entry that has to
stay readable to a v1-only consumer.

A v2 promotion never invents a coordinate. Whatever the candidate recorded as
``unknown`` becomes an explicit unresolved marker, which keeps the entry at
``status: unverified`` and out of any export until ``resolve`` and ``verify``
fill it in from a real run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_knowledge_v2 as v2  # noqa: E402
import vaws_redaction as redaction  # noqa: E402
from vaws_knowledge_v1 import (  # noqa: E402
    COORDINATE_DIMENSIONS,
    COORDINATE_UNKNOWN,
    KNOWLEDGE_FILES,
    KnowledgeError,
    get_knowledge_entry,
    load_candidate,
    load_knowledge_file,
    normalize_fingerprint,
    query_knowledge,
    validate_knowledge_document,
    write_knowledge_document,
)

SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ACTIVE_EVIDENCE_KINDS = frozenset(
    {"test", "regression-test", "acceptance-test"}
)
DEFAULT_ORIGIN_REPO = os.environ.get(
    "VAWS_KNOWLEDGE_ORIGIN_REPO", "unpublished/local-fork"
)
# Evidence kind (candidate v1/v2) -> v2 evidence type. Anything else is not a
# followable upstream reference and is dropped from the v2 evidence array.
_EVIDENCE_TYPE_MAP = {
    "commit": "commit",
    "git": "commit",
    "pr": "pull_request",
    "pull-request": "pull_request",
    "pull_request": "pull_request",
    "issue": "issue",
    "ci": "ci_run",
    "ci-run": "ci_run",
    "run": "run_manifest",
    "run-manifest": "run_manifest",
    "run_manifest": "run_manifest",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def today(now: str | None = None) -> str:
    return (now or utc_now())[:10]


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


def _candidate_path(candidate_dir: Path, candidate_id: str) -> Path:
    if not SAFE_ID_RE.fullmatch(candidate_id):
        raise KnowledgeError("candidate id must be a lowercase safe identifier")
    return candidate_dir / f"{candidate_id}.json"


def list_candidates(candidate_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not candidate_dir.exists():
        return results
    for path in sorted(candidate_dir.glob("*.json")):
        candidate = load_candidate(path)
        results.append(
            {
                "candidate_id": candidate["candidate_id"],
                "kind": candidate["kind"],
                "summary": candidate["summary"],
                "owner_skill": candidate["owner_skill"],
                "confidence": candidate["confidence"],
                "verification_status": candidate["verification"]["status"],
                "occurrence_count": candidate["occurrence_count"],
                "updated_at": candidate["updated_at"],
            }
        )
    return results


def possible_matches(
    candidate: Mapping[str, Any], knowledge_dir: Path, *, limit: int = 3
) -> list[dict[str, Any]]:
    query = " ".join(
        [
            *candidate["fingerprints"],
            candidate["summary"],
            candidate["root_cause"],
        ]
    )
    return query_knowledge(
        knowledge_dir=knowledge_dir,
        query=query,
        kinds=[candidate["kind"]],
        limit=limit,
        include_deprecated=True,
    )


def inspect_candidate(
    candidate_id: str, *, candidate_dir: Path, knowledge_dir: Path
) -> dict[str, Any]:
    candidate = load_candidate(_candidate_path(candidate_dir, candidate_id))
    return {
        "candidate": candidate,
        "possible_matches": possible_matches(candidate, knowledge_dir),
    }


def _stable_evidence(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in candidate["evidence"] if item["stable"]]


def _check_promotion_gate(
    candidate: Mapping[str, Any], status: str
) -> None:
    if status not in {"experimental", "active"}:
        raise KnowledgeError("promotion status must be experimental or active")
    if candidate["verification"]["status"] != "passed":
        raise KnowledgeError("inconclusive candidates cannot be promoted")
    stable = _stable_evidence(candidate)
    if not stable:
        raise KnowledgeError("promotion requires at least one stable evidence item")
    if status == "active":
        has_regression = any(
            item["kind"].lower() in ACTIVE_EVIDENCE_KINDS for item in stable
        )
        if candidate["occurrence_count"] < 2 and not has_regression:
            raise KnowledgeError(
                "active promotion requires two occurrences or stable regression-test evidence"
            )


def _entry_fingerprints(entry: Mapping[str, Any]) -> set[str]:
    rule = entry.get("rule", {})
    if not isinstance(rule, Mapping):
        return set()
    values = rule.get("fingerprints", [])
    if not isinstance(values, list):
        return set()
    return {
        normalize_fingerprint(value)
        for value in values
        if isinstance(value, str)
    }


def _duplicate_fingerprint_entries(
    candidate: Mapping[str, Any], document: Mapping[str, Any]
) -> list[str]:
    candidate_fingerprints = set(candidate["fingerprints"])
    return [
        entry["id"]
        for entry in document["entries"]
        if candidate_fingerprints & _entry_fingerprints(entry)
        and entry["status"] != "deprecated"
    ]


def _candidate_rule(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "candidate_ids": [candidate["candidate_id"]],
        "summary": candidate["summary"],
        "owner_skill": candidate["owner_skill"],
        "scope": deepcopy(candidate["scope"]),
        "fingerprints": list(candidate["fingerprints"]),
        "symptom": candidate["symptom"],
        "root_cause": candidate["root_cause"],
        "resolution": candidate["resolution"],
        "avoidance": candidate["avoidance"],
        "verification": deepcopy(candidate["verification"]),
        "evidence": deepcopy(candidate["evidence"]),
        "confidence": candidate["confidence"],
        "occurrence_count": candidate["occurrence_count"],
        "first_seen_at": candidate["first_seen_at"],
        "last_verified_at": candidate["last_seen_at"],
    }


def _archive_candidate(
    candidate: Mapping[str, Any],
    *,
    candidate_path: Path,
    reviewed_dir: Path,
    disposition: str,
    entry_id: str | None,
    reason: str | None,
    now: str,
) -> Path:
    archive_path = reviewed_dir / f"{candidate['candidate_id']}.json"
    archive = {
        "schema_version": 1,
        "candidate_id": candidate["candidate_id"],
        "disposition": disposition,
        "entry_id": entry_id,
        "reason": reason,
        "reviewed_at": now,
        "candidate": deepcopy(candidate),
    }
    _write_json_atomic(archive_path, archive)
    candidate_path.unlink()
    return archive_path


def promote_candidate(
    candidate_id: str,
    *,
    entry_id: str | None,
    status: str,
    force_new: bool,
    candidate_dir: Path,
    reviewed_dir: Path,
    knowledge_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now()
    candidate_path = _candidate_path(candidate_dir, candidate_id)
    candidate = load_candidate(candidate_path)
    _check_promotion_gate(candidate, status)
    formal_id = entry_id or candidate["candidate_id"]
    if not SAFE_ID_RE.fullmatch(formal_id):
        raise KnowledgeError("entry id must be a lowercase safe identifier")
    filename = next(
        name for name, kind in KNOWLEDGE_FILES.items() if kind == candidate["kind"]
    )
    knowledge_path = knowledge_dir / filename
    document = load_knowledge_file(knowledge_path)
    validate_knowledge_document(
        document, expected_kind=candidate["kind"], path=str(knowledge_path)
    )
    if any(entry["id"] == formal_id for entry in document["entries"]):
        raise KnowledgeError(f"formal entry already exists: {formal_id}")
    duplicates = _duplicate_fingerprint_entries(candidate, document)
    if duplicates and not force_new:
        raise KnowledgeError(
            "matching formal fingerprints already exist; use merge or --force-new: "
            + ", ".join(duplicates)
        )
    stable_uris = [item["uri"] for item in _stable_evidence(candidate)]
    document["entries"].append(
        {
            "id": formal_id,
            "source": (
                f"promoted from candidate {candidate_id}; stable evidence: "
                + ", ".join(stable_uris)
            ),
            "applicable_versions": candidate["applicable_versions"],
            "updated_at": today(timestamp),
            "status": status,
            "rule": _candidate_rule(candidate),
        }
    )
    document["entries"].sort(key=lambda entry: entry["id"])
    document["updated_at"] = today(timestamp)
    write_knowledge_document(knowledge_path, document)
    archive_path = _archive_candidate(
        candidate,
        candidate_path=candidate_path,
        reviewed_dir=reviewed_dir,
        disposition="promoted",
        entry_id=formal_id,
        reason=None,
        now=timestamp,
    )
    return {
        "status": "passed",
        "action": "promoted",
        "candidate_id": candidate_id,
        "entry_id": formal_id,
        "entry_status": status,
        "knowledge_path": str(knowledge_path),
        "archive_path": str(archive_path),
    }


def _unique_values(left: Sequence[Any], right: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in [*left, *right]:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(deepcopy(item))
    return result


def merge_candidate(
    candidate_id: str,
    *,
    entry_id: str,
    candidate_dir: Path,
    reviewed_dir: Path,
    knowledge_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now()
    candidate_path = _candidate_path(candidate_dir, candidate_id)
    candidate = load_candidate(candidate_path)
    _check_promotion_gate(candidate, "experimental")
    found = get_knowledge_entry(knowledge_dir=knowledge_dir, entry_id=entry_id)
    if found is None:
        raise KnowledgeError(f"formal entry does not exist: {entry_id}")
    if found["kind"] != candidate["kind"]:
        raise KnowledgeError("candidate and formal entry kinds do not match")
    knowledge_path = knowledge_dir / found["source_file"]
    document = load_knowledge_file(knowledge_path)
    target = next(entry for entry in document["entries"] if entry["id"] == entry_id)
    rule = target["rule"]
    if not isinstance(rule, dict):
        raise KnowledgeError("formal entry rule must be an object")
    candidate_rule = _candidate_rule(candidate)
    for field in (
        "summary",
        "owner_skill",
        "scope",
        "symptom",
        "root_cause",
        "resolution",
        "avoidance",
        "verification",
        "confidence",
        "last_verified_at",
    ):
        rule[field] = deepcopy(candidate_rule[field])
    rule["candidate_ids"] = _unique_values(
        rule.get("candidate_ids", [rule.get("candidate_id")]),
        [candidate_id],
    )
    rule["candidate_ids"] = [
        value for value in rule["candidate_ids"] if isinstance(value, str)
    ]
    rule.setdefault("candidate_id", rule["candidate_ids"][0])
    rule["fingerprints"] = _unique_values(
        rule.get("fingerprints", []), candidate_rule["fingerprints"]
    )
    rule["evidence"] = _unique_values(
        rule.get("evidence", []), candidate_rule["evidence"]
    )
    rule["occurrence_count"] = int(rule.get("occurrence_count", 1)) + candidate[
        "occurrence_count"
    ]
    rule.setdefault("first_seen_at", candidate["first_seen_at"])
    target["applicable_versions"] = candidate["applicable_versions"]
    target["updated_at"] = today(timestamp)
    target["source"] = (
        target["source"] + f"; merged candidate {candidate_id}"
    )
    document["updated_at"] = today(timestamp)
    write_knowledge_document(knowledge_path, document)
    archive_path = _archive_candidate(
        candidate,
        candidate_path=candidate_path,
        reviewed_dir=reviewed_dir,
        disposition="merged",
        entry_id=entry_id,
        reason=None,
        now=timestamp,
    )
    return {
        "status": "passed",
        "action": "merged",
        "candidate_id": candidate_id,
        "entry_id": entry_id,
        "knowledge_path": str(knowledge_path),
        "archive_path": str(archive_path),
    }


def reject_candidate(
    candidate_id: str,
    *,
    reason: str,
    candidate_dir: Path,
    reviewed_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    if not reason.strip():
        raise KnowledgeError("rejection reason must be non-empty")
    timestamp = now or utc_now()
    candidate_path = _candidate_path(candidate_dir, candidate_id)
    candidate = load_candidate(candidate_path)
    archive_path = _archive_candidate(
        candidate,
        candidate_path=candidate_path,
        reviewed_dir=reviewed_dir,
        disposition="rejected",
        entry_id=None,
        reason=reason.strip(),
        now=timestamp,
    )
    return {
        "status": "passed",
        "action": "rejected",
        "candidate_id": candidate_id,
        "archive_path": str(archive_path),
    }


def deprecate_entry(
    entry_id: str,
    *,
    superseded_by: str | None,
    reason: str,
    knowledge_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    if not reason.strip():
        raise KnowledgeError("deprecation reason must be non-empty")
    found = get_knowledge_entry(knowledge_dir=knowledge_dir, entry_id=entry_id)
    if found is None:
        raise KnowledgeError(f"formal entry does not exist: {entry_id}")
    if superseded_by == entry_id:
        raise KnowledgeError("an entry cannot supersede itself")
    if superseded_by and get_knowledge_entry(
        knowledge_dir=knowledge_dir, entry_id=superseded_by
    ) is None:
        raise KnowledgeError(f"superseding entry does not exist: {superseded_by}")
    timestamp = now or utc_now()
    knowledge_path = knowledge_dir / found["source_file"]
    document = load_knowledge_file(knowledge_path)
    target = next(entry for entry in document["entries"] if entry["id"] == entry_id)
    target["status"] = "deprecated"
    target["updated_at"] = today(timestamp)
    target["rule"]["deprecation"] = {
        "reason": reason.strip(),
        "superseded_by": superseded_by,
        "deprecated_at": today(timestamp),
    }
    document["updated_at"] = today(timestamp)
    write_knowledge_document(knowledge_path, document)
    return {
        "status": "passed",
        "action": "deprecated",
        "entry_id": entry_id,
        "superseded_by": superseded_by,
        "knowledge_path": str(knowledge_path),
    }


# ---------------------------------------------------------------------------
# federated v2 curation
# ---------------------------------------------------------------------------


def _v2_path(knowledge_dir: Path, kind: str) -> Path:
    return knowledge_dir / f"{kind}{v2.V2_SUFFIX}"


def _load_v2_document(path: Path, kind: str, now: str) -> dict[str, Any]:
    if not path.exists():
        return v2.new_document(kind, now=now)
    document = v2.load_document(path)
    v2.validate_document(
        document, path=str(path), expected_kind=kind, context=v2.PROJECT_LAYER
    )
    return document


def _find_v2_entry(document: Mapping[str, Any], entry_id: str) -> dict[str, Any] | None:
    for entry in document["entries"]:
        if entry.get("slug") == entry_id or entry.get("uuid") == entry_id:
            return entry
    return None


def _locate_v2_entry(
    knowledge_dir: Path, entry_id: str, now: str
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    for path, kind in v2.iter_documents(knowledge_dir):
        document = _load_v2_document(path, kind, now)
        entry = _find_v2_entry(document, entry_id)
        if entry is not None:
            return path, document, entry
    raise KnowledgeError(f"v2 entry does not exist: {entry_id}")


def scope_from_environment(
    environment: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Turn a captured coordinate into a v2 ``scope``.

    A concrete value becomes a single-valued ``values`` constraint — the claim
    is asserted exactly where it was observed, and nowhere else. ``unknown``
    becomes an unresolved marker rather than ``any``: ``any`` is a positive
    claim of independence that requires an examined basis, and nobody examined
    anything here.
    """

    scope: dict[str, Any] = {}
    pending: list[dict[str, str]] = []
    for dimension in v2.SCOPE_DIMENSIONS:
        raw = environment.get(dimension)
        value = str(raw).strip() if isinstance(raw, str) else ""
        if value and value != COORDINATE_UNKNOWN:
            scope[dimension] = v2.values_constraint([value])
            continue
        needs = v2.UNRESOLVED_HINTS[dimension]
        scope[dimension] = v2.unresolved_constraint(needs)
        pending.append({"dimension": dimension, "needs": needs})
    return scope, pending


def _v2_rule_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "summary": candidate["summary"],
        "symptom": candidate["symptom"],
        "root_cause": candidate["root_cause"],
        "resolution": candidate["resolution"],
    }
    avoidance = candidate.get("avoidance")
    if isinstance(avoidance, str) and avoidance.strip():
        rule["avoidance"] = avoidance.strip()
    fingerprints = [
        value for value in candidate.get("fingerprints", []) if isinstance(value, str)
    ]
    if fingerprints:
        rule["fingerprints"] = fingerprints
    return rule


def _v2_evidence_from_candidate(
    candidate: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Map stable candidate evidence onto the v2 evidence whitelist.

    v2 only accepts references someone else can follow (run manifest, PR,
    issue, commit, CI run). A local log path is real evidence to the author and
    unusable to a reviewer, so it is dropped and reported, never relabelled.
    """

    evidence: list[dict[str, Any]] = []
    dropped: list[str] = []
    for item in _stable_evidence(candidate):
        mapped = _EVIDENCE_TYPE_MAP.get(str(item.get("kind", "")).lower())
        if mapped is None:
            dropped.append(f"{item.get('kind')}:{item.get('uri')}")
            continue
        entry: dict[str, Any] = {"type": mapped, "ref": item["uri"]}
        note = item.get("note")
        if isinstance(note, str) and note.strip():
            entry["note"] = note.strip()
        evidence.append(entry)
    return evidence, dropped


def _duplicate_v2_slugs(
    candidate: Mapping[str, Any], document: Mapping[str, Any]
) -> list[str]:
    fingerprints = {
        v2.normalize_fingerprints([value])[0]
        for value in candidate["fingerprints"]
        if isinstance(value, str) and value.strip()
    }
    duplicates: list[str] = []
    for entry in document["entries"]:
        if entry.get("status") == "deprecated":
            continue
        if v2.body_key(entry) != "rule":
            continue
        rule = entry.get("rule", {})
        existing = v2.normalize_fingerprints(rule.get("fingerprints", []) or [])
        if fingerprints & set(existing):
            duplicates.append(str(entry.get("slug")))
    return duplicates


def promote_candidate_v2(
    candidate_id: str,
    *,
    entry_id: str | None,
    gate_status: str,
    force_new: bool,
    origin_repo: str,
    contributor: str | None,
    candidate_dir: Path,
    reviewed_dir: Path,
    knowledge_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Promote a candidate into the project-layer v2 document.

    The promoted entry is always ``unverified``. ``verified`` additionally
    requires a complete coordinate and a confirming handle from someone other
    than the submitter, which is what ``verify`` is for.
    """

    timestamp = now or utc_now()
    candidate_path = _candidate_path(candidate_dir, candidate_id)
    candidate = load_candidate(candidate_path)
    _check_promotion_gate(candidate, gate_status)
    slug = entry_id or candidate["candidate_id"]
    if not v2.SLUG_RE.fullmatch(slug):
        raise KnowledgeError("entry id must be a lowercase safe identifier")
    kind = candidate["kind"]
    path = _v2_path(knowledge_dir, kind)
    document = _load_v2_document(path, kind, timestamp)
    if _find_v2_entry(document, slug) is not None:
        raise KnowledgeError(f"v2 entry already exists: {slug}")
    duplicates = _duplicate_v2_slugs(candidate, document)
    if duplicates and not force_new:
        raise KnowledgeError(
            "matching v2 fingerprints already exist; use --force-new after "
            "confirming these are distinct claims: " + ", ".join(duplicates)
        )

    environment = candidate.get("environment") or {}
    scope, pending = scope_from_environment(environment)
    evidence, dropped_evidence = _v2_evidence_from_candidate(candidate)
    confidence = candidate["confidence"]
    if confidence == "high":
        # v2 reserves high confidence for verified/stale/resolved entries.
        confidence = "medium"
    entry = {
        "uuid": v2.derived_uuid(origin_repo, kind, slug),
        "slug": slug,
        "content_hash": "sha256:" + "0" * 64,
        "status": "unverified",
        "confidence": confidence,
        "scope": scope,
        "provenance": {
            "contributor": contributor or candidate["owner_skill"],
            "origin_repo": origin_repo,
            "submitted_at": v2.today(timestamp),
            "redaction_profile": redaction.REDACTION_PROFILE,
        },
        "lifecycle": {
            "first_seen": v2.today(candidate["first_seen_at"]),
            "updated_at": v2.today(timestamp),
            "superseded_by": None,
            "resolved_by": None,
        },
        "rule": _v2_rule_from_candidate(candidate),
    }
    if evidence:
        entry["verification"] = {
            "evidence": evidence,
            "verified_by": [],
            "verified_against": {},
            "last_verified_at": v2.today(candidate["last_seen_at"]),
        }
        # verified_against must be a complete concrete environment; an
        # incomplete one is not carried at all rather than half-filled.
        if all(
            isinstance(environment.get(field), str)
            and environment[field] != COORDINATE_UNKNOWN
            for field in v2.CONCRETE_REQUIRED
        ):
            entry["verification"]["verified_against"] = {
                field: environment[field] for field in v2.CONCRETE_REQUIRED
            }
        else:
            entry.pop("verification")
            dropped_evidence.extend(
                f"{item['type']}:{item['ref']}" for item in evidence
            )
    entry = v2.with_content_hash(entry)
    errors = v2.validate_entry(entry, path=slug, context=v2.PROJECT_LAYER)
    if errors:
        raise KnowledgeError("; ".join(errors))
    document["entries"].append(entry)
    document["entries"].sort(key=lambda item: item["slug"])
    document["updated_at"] = v2.today(timestamp)
    v2.write_document(path, document)
    archive_path = _archive_candidate(
        candidate,
        candidate_path=candidate_path,
        reviewed_dir=reviewed_dir,
        disposition="promoted-v2",
        entry_id=slug,
        reason=None,
        now=timestamp,
    )
    return {
        "status": "passed",
        "action": "promoted",
        "schema_version": v2.SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "entry_id": slug,
        "entry_uuid": entry["uuid"],
        "entry_status": entry["status"],
        "promotion_gate": gate_status,
        "knowledge_path": str(path),
        "archive_path": str(archive_path),
        "needs_human_input": pending,
        "dropped_evidence": dropped_evidence,
        "exportable": not pending,
        "next_steps": _next_steps(slug, pending),
    }


def _next_steps(slug: str, pending: Sequence[Mapping[str, str]]) -> list[str]:
    steps: list[str] = []
    if pending:
        steps.append(
            f"resolve {len(pending)} unresolved dimension(s) with "
            f"'knowledge_curate.py resolve --entry-id {slug} --dimension <name> ...'"
        )
    steps.append(
        f"record independent confirmation with 'knowledge_curate.py verify "
        f"--entry-id {slug} ...' before the entry can be exported as verified"
    )
    steps.append(
        "export with '.agents/scripts/knowledge_export.py --entry-id " + slug + "'"
    )
    return steps


def resolve_dimension(
    entry_id: str,
    *,
    dimension: str,
    values: Sequence[str] | None,
    basis: str | None,
    minimum: str | None,
    maximum: str | None,
    knowledge_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Fill one coordinate dimension with a human-supplied constraint."""

    timestamp = now or utc_now()
    if dimension not in v2.SCOPE_DIMENSIONS:
        raise KnowledgeError(
            "dimension must be one of: " + ", ".join(v2.SCOPE_DIMENSIONS)
        )
    supplied = [bool(values), bool(basis), bool(minimum or maximum)]
    if sum(supplied) != 1:
        raise KnowledgeError(
            "supply exactly one of --values, --any-basis, or --min/--max"
        )
    if values:
        constraint = v2.values_constraint(values)
    elif basis:
        constraint = v2.any_constraint(basis)
    else:
        constraint = v2.range_constraint(minimum, maximum)
    path, document, entry = _locate_v2_entry(knowledge_dir, entry_id, timestamp)
    was_unresolved = v2.is_unresolved(entry["scope"].get(dimension))
    entry["scope"][dimension] = constraint
    entry["lifecycle"]["updated_at"] = v2.today(timestamp)
    entry.update(v2.with_content_hash(entry))
    errors = v2.validate_entry(entry, path=entry["slug"], context=v2.PROJECT_LAYER)
    if errors:
        raise KnowledgeError("; ".join(errors))
    document["updated_at"] = v2.today(timestamp)
    v2.write_document(path, document)
    remaining = v2.unresolved_dimensions(entry)
    return {
        "status": "passed",
        "action": "resolved",
        "entry_id": entry["slug"],
        "dimension": dimension,
        "was_unresolved": was_unresolved,
        "constraint": constraint,
        "content_hash": entry["content_hash"],
        "unresolved_remaining": remaining,
        "knowledge_path": str(path),
    }


def verify_entry(
    entry_id: str,
    *,
    evidence: Sequence[str],
    verified_by: Sequence[str],
    environment: Mapping[str, str],
    verified_at: str | None,
    knowledge_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Attach independent confirmation and move a v2 entry to ``verified``.

    Refused while any dimension is unresolved: a claim whose applicability is
    unknown cannot be verified, only observed.
    """

    timestamp = now or utc_now()
    path, document, entry = _locate_v2_entry(knowledge_dir, entry_id, timestamp)
    pending = v2.unresolved_dimensions(entry)
    if pending:
        raise KnowledgeError(
            "cannot verify while dimensions are unresolved: " + ", ".join(pending)
        )
    parsed: list[dict[str, Any]] = []
    for item in evidence:
        kind, _, ref = item.partition(":")
        mapped = _EVIDENCE_TYPE_MAP.get(kind.strip().lower(), kind.strip().lower())
        if mapped not in v2.EVIDENCE_TYPES or not ref.strip():
            raise KnowledgeError(
                "evidence must be '<type>:<ref>' with type in "
                + ", ".join(sorted(v2.EVIDENCE_TYPES))
            )
        parsed.append({"type": mapped, "ref": ref.strip()})
    if not parsed:
        raise KnowledgeError("verification requires at least one evidence reference")
    handles = [value.strip() for value in verified_by if value.strip()]
    if not handles:
        raise KnowledgeError("verification requires at least one confirming handle")
    submitter = entry["provenance"]["contributor"]
    if handles == [submitter]:
        raise KnowledgeError(
            "verified_by repeats the submitter; verification needs a second party"
        )
    missing = [
        field
        for field in v2.CONCRETE_REQUIRED
        if not str(environment.get(field, "")).strip()
        or environment.get(field) == COORDINATE_UNKNOWN
    ]
    if missing:
        raise KnowledgeError(
            "verified_against needs concrete values for: " + ", ".join(missing)
        )
    verified_against = {
        field: str(environment[field]).strip()
        for field in (*v2.CONCRETE_REQUIRED, *v2.CONCRETE_OPTIONAL)
        if str(environment.get(field, "")).strip()
        and environment.get(field) != COORDINATE_UNKNOWN
    }
    entry["verification"] = {
        "evidence": parsed,
        "verified_by": handles,
        "verified_against": verified_against,
        "last_verified_at": v2.today(verified_at or timestamp),
    }
    entry["status"] = "verified"
    entry["lifecycle"]["updated_at"] = v2.today(timestamp)
    entry.update(v2.with_content_hash(entry))
    errors = v2.validate_entry(entry, path=entry["slug"], context="export")
    if errors:
        raise KnowledgeError("; ".join(errors))
    document["updated_at"] = v2.today(timestamp)
    v2.write_document(path, document)
    return {
        "status": "passed",
        "action": "verified",
        "entry_id": entry["slug"],
        "entry_status": entry["status"],
        "content_hash": entry["content_hash"],
        "knowledge_path": str(path),
        "exportable": True,
    }


def deprecate_entry_v2(
    entry_id: str,
    *,
    superseded_by: str | None,
    reason: str,
    knowledge_dir: Path,
    reviewed_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Deprecate a v2 entry.

    v2 has no field for a deprecation reason (only ``superseded_by`` and
    ``resolved_by``), so the reason is archived locally under
    ``.vaws-local/`` instead of being smuggled into the rule prose.
    """

    if not reason.strip():
        raise KnowledgeError("deprecation reason must be non-empty")
    timestamp = now or utc_now()
    path, document, entry = _locate_v2_entry(knowledge_dir, entry_id, timestamp)
    if superseded_by:
        target = _find_v2_entry(document, superseded_by)
        if target is None:
            for other_path, kind in v2.iter_documents(knowledge_dir):
                if other_path == path:
                    continue
                target = _find_v2_entry(
                    _load_v2_document(other_path, kind, timestamp), superseded_by
                )
                if target is not None:
                    break
        if target is None:
            raise KnowledgeError(f"superseding entry does not exist: {superseded_by}")
        if target["uuid"] == entry["uuid"]:
            raise KnowledgeError("an entry cannot supersede itself")
        entry["lifecycle"]["superseded_by"] = target["uuid"]
    entry["status"] = "deprecated"
    if entry["confidence"] == "high":
        entry["confidence"] = "medium"
    entry["lifecycle"]["updated_at"] = v2.today(timestamp)
    entry.update(v2.with_content_hash(entry))
    errors = v2.validate_entry(entry, path=entry["slug"], context=v2.PROJECT_LAYER)
    if errors:
        raise KnowledgeError("; ".join(errors))
    document["updated_at"] = v2.today(timestamp)
    v2.write_document(path, document)
    note_path = reviewed_dir / f"{entry['slug']}.deprecation.json"
    _write_json_atomic(
        note_path,
        {
            "schema_version": 1,
            "entry_uuid": entry["uuid"],
            "entry_slug": entry["slug"],
            "reason": reason.strip(),
            "superseded_by": entry["lifecycle"]["superseded_by"],
            "deprecated_at": v2.today(timestamp),
        },
    )
    return {
        "status": "passed",
        "action": "deprecated",
        "schema_version": v2.SCHEMA_VERSION,
        "entry_id": entry["slug"],
        "superseded_by": entry["lifecycle"]["superseded_by"],
        "knowledge_path": str(path),
        "reason_path": str(note_path),
    }


def list_unresolved(knowledge_dir: Path) -> dict[str, Any]:
    """Report every v2 entry still waiting on a human coordinate."""

    entries, problems = v2.load_entries(knowledge_dir, context=v2.PROJECT_LAYER)
    pending: list[dict[str, Any]] = []
    for entry in entries:
        dimensions = v2.unresolved_dimensions(entry)
        if not dimensions:
            continue
        pending.append(
            {
                "entry_id": entry["slug"],
                "kind": entry.get("_kind"),
                "status": entry["status"],
                "summary": v2.entry_summary(entry),
                "unresolved": [
                    {
                        "dimension": name,
                        "needs": entry["scope"][name].get("needs", ""),
                    }
                    for name in dimensions
                ],
            }
        )
    return {
        "status": "passed",
        "action": "list-unresolved",
        "entries_total": len(entries),
        "entries_blocked": len(pending),
        "blocked": pending,
        "problems": problems,
    }


def _parse_env_pairs(pairs: Sequence[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or key not in COORDINATE_DIMENSIONS:
            raise KnowledgeError(
                f"--env expects <dimension>=<value> with dimension in: "
                + ", ".join(COORDINATE_DIMENSIONS)
            )
        environment[key] = value.strip()
    return environment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "candidates",
    )
    parser.add_argument(
        "--reviewed-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "reviewed",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--candidate-id", required=True)
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--candidate-id", required=True)
    promote_parser.add_argument("--entry-id")
    promote_parser.add_argument(
        "--status",
        choices=["experimental", "active"],
        default="experimental",
        help=(
            "v1 entry status; with --schema 2 this only selects the evidence "
            "gate, because a promoted v2 entry is always 'unverified'"
        ),
    )
    promote_parser.add_argument("--force-new", action="store_true")
    promote_parser.add_argument(
        "--schema",
        type=int,
        choices=[1, 2],
        default=2,
        help="write the federated v2 entry (default) or the legacy v1 entry",
    )
    promote_parser.add_argument("--origin-repo", default=DEFAULT_ORIGIN_REPO)
    promote_parser.add_argument("--contributor")
    merge_parser = subparsers.add_parser("merge")
    merge_parser.add_argument("--candidate-id", required=True)
    merge_parser.add_argument("--entry-id", required=True)
    reject_parser = subparsers.add_parser("reject")
    reject_parser.add_argument("--candidate-id", required=True)
    reject_parser.add_argument("--reason", required=True)
    deprecate_parser = subparsers.add_parser("deprecate")
    deprecate_parser.add_argument("--entry-id", required=True)
    deprecate_parser.add_argument("--superseded-by")
    deprecate_parser.add_argument("--reason", required=True)
    deprecate_parser.add_argument(
        "--schema", type=int, choices=[1, 2], default=2
    )
    resolve_parser = subparsers.add_parser(
        "resolve", help="fill one unresolved v2 coordinate dimension"
    )
    resolve_parser.add_argument("--entry-id", required=True)
    resolve_parser.add_argument(
        "--dimension", required=True, choices=list(v2.SCOPE_DIMENSIONS)
    )
    resolve_parser.add_argument("--values", nargs="+")
    resolve_parser.add_argument(
        "--any-basis",
        help="declare independence, stating what was examined to establish it",
    )
    resolve_parser.add_argument("--min")
    resolve_parser.add_argument("--max")
    verify_parser = subparsers.add_parser(
        "verify", help="attach independent confirmation to a v2 entry"
    )
    verify_parser.add_argument("--entry-id", required=True)
    verify_parser.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="TYPE:REF",
        help="run_manifest|pull_request|issue|commit|ci_run followed by a reference",
    )
    verify_parser.add_argument("--verified-by", action="append", default=[])
    verify_parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="DIM=VALUE",
        help="the environment the confirmation actually ran on",
    )
    verify_parser.add_argument("--verified-at")
    subparsers.add_parser(
        "list-unresolved", help="v2 entries still waiting on a human coordinate"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            payload = {
                "status": "passed",
                "candidates": list_candidates(args.candidate_dir),
            }
        elif args.command == "inspect":
            payload = {
                "status": "passed",
                **inspect_candidate(
                    args.candidate_id,
                    candidate_dir=args.candidate_dir,
                    knowledge_dir=args.knowledge_dir,
                ),
            }
        elif args.command == "promote":
            if args.schema == 2:
                payload = promote_candidate_v2(
                    args.candidate_id,
                    entry_id=args.entry_id,
                    gate_status=args.status,
                    force_new=args.force_new,
                    origin_repo=args.origin_repo,
                    contributor=args.contributor,
                    candidate_dir=args.candidate_dir,
                    reviewed_dir=args.reviewed_dir,
                    knowledge_dir=args.knowledge_dir,
                )
            else:
                payload = promote_candidate(
                    args.candidate_id,
                    entry_id=args.entry_id,
                    status=args.status,
                    force_new=args.force_new,
                    candidate_dir=args.candidate_dir,
                    reviewed_dir=args.reviewed_dir,
                    knowledge_dir=args.knowledge_dir,
                )
        elif args.command == "merge":
            payload = merge_candidate(
                args.candidate_id,
                entry_id=args.entry_id,
                candidate_dir=args.candidate_dir,
                reviewed_dir=args.reviewed_dir,
                knowledge_dir=args.knowledge_dir,
            )
        elif args.command == "reject":
            payload = reject_candidate(
                args.candidate_id,
                reason=args.reason,
                candidate_dir=args.candidate_dir,
                reviewed_dir=args.reviewed_dir,
            )
        elif args.command == "resolve":
            payload = resolve_dimension(
                args.entry_id,
                dimension=args.dimension,
                values=args.values,
                basis=args.any_basis,
                minimum=args.min,
                maximum=args.max,
                knowledge_dir=args.knowledge_dir,
            )
        elif args.command == "verify":
            payload = verify_entry(
                args.entry_id,
                evidence=args.evidence,
                verified_by=args.verified_by,
                environment=_parse_env_pairs(args.env),
                verified_at=args.verified_at,
                knowledge_dir=args.knowledge_dir,
            )
        elif args.command == "list-unresolved":
            payload = list_unresolved(args.knowledge_dir)
        elif args.schema == 2:
            payload = deprecate_entry_v2(
                args.entry_id,
                superseded_by=args.superseded_by,
                reason=args.reason,
                knowledge_dir=args.knowledge_dir,
                reviewed_dir=args.reviewed_dir,
            )
        else:
            payload = deprecate_entry(
                args.entry_id,
                superseded_by=args.superseded_by,
                reason=args.reason,
                knowledge_dir=args.knowledge_dir,
            )
    except (KnowledgeError, v2.KnowledgeV2Error, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
