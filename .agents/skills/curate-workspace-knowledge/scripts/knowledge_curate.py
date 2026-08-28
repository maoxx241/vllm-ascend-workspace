#!/usr/bin/env python3
"""Review and curate verified workspace knowledge candidates."""

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

from vaws_knowledge import (  # noqa: E402
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
        "--status", choices=["experimental", "active"], default="experimental"
    )
    promote_parser.add_argument("--force-new", action="store_true")
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
        else:
            payload = deprecate_entry(
                args.entry_id,
                superseded_by=args.superseded_by,
                reason=args.reason,
                knowledge_dir=args.knowledge_dir,
            )
    except (KnowledgeError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
