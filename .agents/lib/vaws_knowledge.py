#!/usr/bin/env python3
"""Validate, capture, and query workspace knowledge without loading a Skill."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 1
KNOWLEDGE_FILES = {
    "version-compatibility.yaml": "version-compatibility",
    "model-capabilities.yaml": "model-capabilities",
    "parallelism-compatibility.yaml": "parallelism-compatibility",
    "backend-constraints.yaml": "backend-constraints",
    "validation-rules.yaml": "validation-rules",
    "known-failure-signatures.yaml": "known-failure-signatures",
}
KNOWLEDGE_KINDS = frozenset(KNOWLEDGE_FILES.values())
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
SAFE_ENTRY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SAFE_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ENTRY_STATUSES = frozenset({"active", "deprecated", "experimental"})
CANDIDATE_CONFIDENCE = frozenset({"low", "medium", "high"})
VERIFICATION_STATUSES = frozenset({"passed", "inconclusive"})
SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|auth|credential|pass(?:word)?|secret|token)(?:_|$)",
    re.IGNORECASE,
)
SECRET_VALUE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
)
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
TOKEN_RE = re.compile(r"[\w.-]{2,}", re.UNICODE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
HEX_ADDRESS_RE = re.compile(r"\b0x[0-9a-f]{6,}\b", re.IGNORECASE)
TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+(?:Z|[+-]\d{2}:?\d{2})?\b",
    re.IGNORECASE,
)
PID_RE = re.compile(r"\b(pid|process)\s*[:=#]?\s*\d+\b", re.IGNORECASE)
KNOWN_WORKSPACE_PATH_RE = re.compile(
    r"(?:/home/[^/\s]+/[^/\s]+/|/vllm-workspace/|[A-Za-z]:[\\/]Users[\\/][^\\/\s]+[\\/][^\\/\s]+[\\/])"
)
URI_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
ALLOWED_EVIDENCE_SCHEMES = frozenset({"https", "http", "run", "commit", "git"})
MAX_CANDIDATE_BYTES = 64 * 1024
MAX_NARRATIVE_LENGTH = 4000
MAX_FINGERPRINT_LENGTH = 1000


class KnowledgeError(ValueError):
    """Raised when a knowledge file violates the v1 contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def knowledge_session_key(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise KnowledgeError("session id must be a non-empty string")
    return hashlib.sha256(session_id.strip().encode("utf-8")).hexdigest()[:20]


def _require_non_empty_string(value: Any, path: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path} must be a non-empty string")
        return ""
    return value.strip()


def _require_string_array(
    value: Any, path: str, errors: list[str], *, non_empty: bool = False
) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        errors.append(f"{path} must be an array of non-empty strings")
        return []
    if non_empty and not value:
        errors.append(f"{path} must contain at least one item")
    return [item.strip() for item in value]


def _scan_sensitive(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                errors.append(f"{path} contains a non-string key")
                continue
            if SECRET_KEY_RE.search(key):
                errors.append(f"{path}.{key} uses a secret-like key")
            _scan_sensitive(child, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_sensitive(child, f"{path}[{index}]", errors)
    elif isinstance(value, str):
        for pattern in SECRET_VALUE_RES:
            if pattern.search(value):
                errors.append(f"{path} contains a secret-like value")
                break


def _validate_evidence_uri(uri: str, path: str, errors: list[str]) -> None:
    if uri.startswith("file://") or Path(uri).is_absolute() or WINDOWS_ABSOLUTE_RE.match(uri):
        errors.append(f"{path} must be a repository-relative path or stable external URI")
        return
    scheme = URI_SCHEME_RE.match(uri)
    if scheme:
        if scheme.group(1).lower() not in ALLOWED_EVIDENCE_SCHEMES:
            errors.append(f"{path} uses an unsupported URI scheme")
        return
    if ".." in Path(uri).parts:
        errors.append(f"{path} must not traverse outside the repository")


def normalize_fingerprint(value: str) -> str:
    normalized = ANSI_RE.sub("", value)
    normalized = UUID_RE.sub("<uuid>", normalized)
    normalized = HEX_ADDRESS_RE.sub("<address>", normalized)
    normalized = TIMESTAMP_RE.sub("<timestamp>", normalized)
    normalized = PID_RE.sub(lambda match: f"{match.group(1).lower()}=<pid>", normalized)
    normalized = KNOWN_WORKSPACE_PATH_RE.sub("<workspace>/", normalized)
    return " ".join(normalized.lower().split())


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


def load_knowledge_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeError(
            f"{path} must use JSON-compatible YAML so stdlib validation works: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise KnowledgeError(f"{path}: document root must be an object")
    return payload


def validate_knowledge_document(
    payload: Mapping[str, Any], *, expected_kind: str, path: str
) -> None:
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if payload.get("kind") != expected_kind:
        errors.append(f"kind must be {expected_kind!r}")
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str) or not DATE_RE.fullmatch(updated_at):
        errors.append("updated_at must use YYYY-MM-DD")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        errors.append("entries must be an array")
        entries = []

    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        prefix = f"entries[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not SAFE_ENTRY_ID_RE.fullmatch(entry_id):
            errors.append(f"{prefix}.id must be a lowercase safe identifier")
        elif entry_id in seen_ids:
            errors.append(f"{prefix}.id is duplicated: {entry_id}")
        else:
            seen_ids.add(entry_id)
        for field in ("source", "applicable_versions", "updated_at"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                errors.append(f"{prefix}.{field} must be a non-empty string")
        entry_date = entry.get("updated_at")
        if isinstance(entry_date, str) and not DATE_RE.fullmatch(entry_date):
            errors.append(f"{prefix}.updated_at must use YYYY-MM-DD")
        if entry.get("status") not in ENTRY_STATUSES:
            errors.append(
                f"{prefix}.status must be one of: {', '.join(sorted(ENTRY_STATUSES))}"
            )
        if "rule" not in entry or not isinstance(entry["rule"], Mapping):
            errors.append(f"{prefix}.rule must be an object")

    allowed = {"schema_version", "kind", "updated_at", "entries"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        errors.append(f"unknown top-level fields: {', '.join(unknown)}")
    if errors:
        raise KnowledgeError(f"{path}: " + "; ".join(errors))


def validate_knowledge_dir(root: Path) -> list[str]:
    validated: list[str] = []
    errors: list[str] = []
    for filename, kind in KNOWLEDGE_FILES.items():
        path = root / filename
        if not path.is_file():
            errors.append(f"missing knowledge file: {path}")
            continue
        try:
            payload = load_knowledge_file(path)
            validate_knowledge_document(payload, expected_kind=kind, path=str(path))
        except KnowledgeError as exc:
            errors.append(str(exc))
        else:
            validated.append(filename)
    unknown = sorted(
        path.name for path in root.glob("*.yaml") if path.name not in KNOWLEDGE_FILES
    )
    if unknown:
        errors.append(f"unknown knowledge files: {', '.join(unknown)}")
    if errors:
        raise KnowledgeError("\n".join(errors))
    return validated


def generate_candidate_id(payload: Mapping[str, Any]) -> str:
    owner = str(payload.get("owner_skill", "knowledge")).lower()
    owner = re.sub(r"[^a-z0-9-]+", "-", owner).strip("-") or "knowledge"
    summary = str(payload.get("summary", "candidate")).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", summary).strip("-")[:48] or "candidate"
    semantic = {
        "kind": payload.get("kind"),
        "owner_skill": payload.get("owner_skill"),
        "scope": payload.get("scope", {}),
        "fingerprints": sorted(
            normalize_fingerprint(str(item))
            for item in payload.get("fingerprints", [])
        ),
        "symptom": normalize_fingerprint(str(payload.get("symptom", ""))),
        "root_cause": normalize_fingerprint(str(payload.get("root_cause", ""))),
        "applicable_versions": str(payload.get("applicable_versions", "")).strip().lower(),
    }
    digest = hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    return f"{owner[:32]}-{slug}-{digest}"[:128].rstrip("-")


def normalize_candidate(
    payload: Mapping[str, Any], *, now: str | None = None
) -> dict[str, Any]:
    timestamp = now or utc_now()
    normalized = deepcopy(dict(payload))
    if isinstance(normalized.get("fingerprints"), list):
        normalized["fingerprints"] = list(
            dict.fromkeys(
                normalize_fingerprint(item)
                for item in normalized["fingerprints"]
                if isinstance(item, str)
            )
        )
    normalized.setdefault("schema_version", CANDIDATE_SCHEMA_VERSION)
    normalized.setdefault("candidate_id", generate_candidate_id(normalized))
    normalized.setdefault("avoidance", "")
    normalized.setdefault("status", "candidate")
    normalized.setdefault("occurrence_count", 1)
    normalized.setdefault("source", {})
    normalized.setdefault("first_seen_at", timestamp)
    normalized.setdefault("last_seen_at", timestamp)
    normalized.setdefault("created_at", timestamp)
    normalized.setdefault("updated_at", timestamp)
    validate_candidate(normalized)
    return normalized


def validate_candidate(payload: Mapping[str, Any], *, path: str = "candidate") -> None:
    errors: list[str] = []
    try:
        serialized_size = len(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
    except (TypeError, ValueError):
        serialized_size = MAX_CANDIDATE_BYTES + 1
    if serialized_size > MAX_CANDIDATE_BYTES:
        errors.append(f"candidate must not exceed {MAX_CANDIDATE_BYTES} bytes")
    if payload.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {CANDIDATE_SCHEMA_VERSION}")
    candidate_id = payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not SAFE_ENTRY_ID_RE.fullmatch(candidate_id):
        errors.append("candidate_id must be a lowercase safe identifier")
    if payload.get("kind") not in KNOWLEDGE_KINDS:
        errors.append(f"kind must be one of: {', '.join(sorted(KNOWLEDGE_KINDS))}")
    for field in (
        "summary",
        "symptom",
        "root_cause",
        "resolution",
        "applicable_versions",
    ):
        text = _require_non_empty_string(payload.get(field), field, errors)
        if len(text) > MAX_NARRATIVE_LENGTH:
            errors.append(f"{field} must not exceed {MAX_NARRATIVE_LENGTH} characters")
    avoidance = payload.get("avoidance")
    if not isinstance(avoidance, str):
        errors.append("avoidance must be a string")
    owner_skill = payload.get("owner_skill")
    if not isinstance(owner_skill, str) or not SAFE_SKILL_RE.fullmatch(owner_skill):
        errors.append("owner_skill must be a lowercase skill-style identifier")
    if not isinstance(payload.get("scope"), Mapping):
        errors.append("scope must be an object")
    fingerprints = _require_string_array(
        payload.get("fingerprints"), "fingerprints", errors, non_empty=True
    )
    for index, fingerprint in enumerate(fingerprints):
        if len(fingerprint) > MAX_FINGERPRINT_LENGTH:
            errors.append(
                f"fingerprints[{index}] must not exceed {MAX_FINGERPRINT_LENGTH} characters"
            )

    verification = payload.get("verification")
    if not isinstance(verification, Mapping):
        errors.append("verification must be an object")
    else:
        unknown = sorted(set(verification) - {"status", "checks"})
        if unknown:
            errors.append(f"verification has unknown fields: {', '.join(unknown)}")
        if verification.get("status") not in VERIFICATION_STATUSES:
            errors.append(
                "verification.status must be one of: "
                + ", ".join(sorted(VERIFICATION_STATUSES))
            )
        _require_string_array(
            verification.get("checks"), "verification.checks", errors, non_empty=True
        )

    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must contain at least one item")
        evidence = []
    for index, item in enumerate(evidence):
        item_path = f"evidence[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{item_path} must be an object")
            continue
        unknown = sorted(set(item) - {"kind", "uri", "stable", "sha256"})
        if unknown:
            errors.append(f"{item_path} has unknown fields: {', '.join(unknown)}")
        _require_non_empty_string(item.get("kind"), f"{item_path}.kind", errors)
        uri = _require_non_empty_string(item.get("uri"), f"{item_path}.uri", errors)
        if uri:
            _validate_evidence_uri(uri, f"{item_path}.uri", errors)
        if not isinstance(item.get("stable"), bool):
            errors.append(f"{item_path}.stable must be a boolean")
        sha256 = item.get("sha256")
        if sha256 is not None and (
            not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256)
        ):
            errors.append(f"{item_path}.sha256 must be 64 lowercase hex characters")

    if payload.get("confidence") not in CANDIDATE_CONFIDENCE:
        errors.append(
            "confidence must be one of: " + ", ".join(sorted(CANDIDATE_CONFIDENCE))
        )
    if payload.get("status") != "candidate":
        errors.append("status must be 'candidate'")
    occurrence_count = payload.get("occurrence_count")
    if not isinstance(occurrence_count, int) or isinstance(occurrence_count, bool) or occurrence_count < 1:
        errors.append("occurrence_count must be an integer greater than zero")

    source = payload.get("source")
    if not isinstance(source, Mapping):
        errors.append("source must be an object")
    else:
        unknown = sorted(set(source) - {"session_id", "run_ids", "commits"})
        if unknown:
            errors.append(f"source has unknown fields: {', '.join(unknown)}")
        session_id = source.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            errors.append("source.session_id must be a string")
        for field in ("run_ids", "commits"):
            if field in source:
                _require_string_array(source[field], f"source.{field}", errors)

    for field in ("first_seen_at", "last_seen_at", "created_at", "updated_at"):
        value = payload.get(field)
        if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
            errors.append(f"{field} must be an RFC3339 UTC timestamp ending in Z")

    allowed = {
        "schema_version",
        "candidate_id",
        "kind",
        "summary",
        "owner_skill",
        "scope",
        "fingerprints",
        "symptom",
        "root_cause",
        "resolution",
        "avoidance",
        "applicable_versions",
        "verification",
        "evidence",
        "confidence",
        "status",
        "occurrence_count",
        "source",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        errors.append(f"unknown top-level fields: {', '.join(unknown)}")
    missing = sorted(allowed - set(payload))
    if missing:
        errors.append(f"missing top-level fields: {', '.join(missing)}")
    _scan_sensitive(payload, path, errors)
    if errors:
        raise KnowledgeError(f"{path}: " + "; ".join(errors))


def load_candidate(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeError(f"cannot read candidate {path}: {exc}") from exc
    if not isinstance(payload, MutableMapping):
        raise KnowledgeError(f"{path}: candidate root must be an object")
    validate_candidate(payload, path=str(path))
    return dict(payload)


def _merge_unique(
    left: Sequence[Any], right: Sequence[Any]
) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for item in [*left, *right]:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            merged.append(deepcopy(item))
    return merged


def _formal_candidate_ids(knowledge_dir: Path) -> set[str]:
    candidate_ids: set[str] = set()
    for filename in KNOWLEDGE_FILES:
        document = load_knowledge_file(knowledge_dir / filename)
        for entry in document.get("entries", []):
            rule = entry.get("rule", {})
            if isinstance(rule, Mapping) and isinstance(rule.get("candidate_id"), str):
                candidate_ids.add(rule["candidate_id"])
            if isinstance(rule, Mapping) and isinstance(rule.get("candidate_ids"), list):
                candidate_ids.update(
                    value for value in rule["candidate_ids"] if isinstance(value, str)
                )
    return candidate_ids


def capture_candidate(
    payload: Mapping[str, Any],
    *,
    candidate_dir: Path,
    knowledge_dir: Path,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now()
    candidate = normalize_candidate(payload, now=timestamp)
    candidate_id = candidate["candidate_id"]
    if candidate_id in _formal_candidate_ids(knowledge_dir):
        return {
            "status": "already-promoted",
            "candidate_id": candidate_id,
            "path": None,
        }

    path = candidate_dir / f"{candidate_id}.json"
    action = "created"
    if path.exists():
        existing = load_candidate(path)
        merged = deepcopy(existing)
        semantic_changed = False
        for field in (
            "summary",
            "scope",
            "symptom",
            "root_cause",
            "resolution",
            "avoidance",
            "applicable_versions",
            "verification",
        ):
            if existing[field] != candidate[field]:
                semantic_changed = True
            merged[field] = deepcopy(candidate[field])
        merged_fingerprints = _merge_unique(
            existing["fingerprints"], candidate["fingerprints"]
        )
        merged_evidence = _merge_unique(existing["evidence"], candidate["evidence"])
        if merged_fingerprints != existing["fingerprints"]:
            semantic_changed = True
        if merged_evidence != existing["evidence"]:
            semantic_changed = True
        merged["fingerprints"] = merged_fingerprints
        merged["evidence"] = merged_evidence
        new_occurrence = len(merged_evidence) > len(existing["evidence"])
        for field in ("run_ids", "commits"):
            merged_values = _merge_unique(
                existing["source"].get(field, []), candidate["source"].get(field, [])
            )
            if len(merged_values) > len(existing["source"].get(field, [])):
                new_occurrence = True
                semantic_changed = True
            merged["source"][field] = merged_values
        if candidate["source"].get("session_id"):
            if candidate["source"]["session_id"] != existing["source"].get("session_id"):
                new_occurrence = True
                semantic_changed = True
            merged["source"]["session_id"] = candidate["source"]["session_id"]
        confidence_order = {"low": 0, "medium": 1, "high": 2}
        if confidence_order[candidate["confidence"]] > confidence_order[existing["confidence"]]:
            merged["confidence"] = candidate["confidence"]
            semantic_changed = True
        if new_occurrence:
            merged["occurrence_count"] = existing["occurrence_count"] + 1
            merged["last_seen_at"] = timestamp
        if semantic_changed:
            merged["updated_at"] = timestamp
        candidate = merged
        action = "updated" if semantic_changed else "unchanged"
    validate_candidate(candidate)
    _write_json_atomic(path, candidate)
    return {
        "status": "passed",
        "action": action,
        "candidate_id": candidate_id,
        "path": str(path),
        "occurrence_count": candidate["occurrence_count"],
    }


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_strings(child)


def _tokens(value: str) -> set[str]:
    return set(TOKEN_RE.findall(value.lower()))


def _entry_score(query: str, entry: Mapping[str, Any]) -> int:
    query_normalized = normalize_fingerprint(query)
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0
    rule = entry.get("rule", {})
    fingerprint_text = normalize_fingerprint(
        " ".join(str(item) for item in rule.get("fingerprints", []))
    ) if isinstance(rule, Mapping) else ""
    score = 0
    if query_normalized and query_normalized in fingerprint_text:
        score += 100
    fingerprint_tokens = _tokens(fingerprint_text)
    score += 12 * len(query_tokens & fingerprint_tokens)
    for field in ("summary", "symptom", "root_cause", "resolution"):
        value = rule.get(field, "") if isinstance(rule, Mapping) else ""
        score += 4 * len(query_tokens & _tokens(str(value)))
    general_text = " ".join(_iter_strings(entry))
    score += len(query_tokens & _tokens(general_text))
    return score


def query_knowledge(
    *,
    knowledge_dir: Path,
    query: str,
    kinds: Sequence[str] | None = None,
    limit: int = 3,
    include_deprecated: bool = False,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise KnowledgeError("limit must be greater than zero")
    selected = set(kinds or KNOWLEDGE_KINDS)
    unknown = sorted(selected - KNOWLEDGE_KINDS)
    if unknown:
        raise KnowledgeError(f"unknown knowledge kinds: {', '.join(unknown)}")
    matches: list[dict[str, Any]] = []
    for filename, kind in KNOWLEDGE_FILES.items():
        if kind not in selected:
            continue
        document = load_knowledge_file(knowledge_dir / filename)
        validate_knowledge_document(
            document, expected_kind=kind, path=str(knowledge_dir / filename)
        )
        for entry in document["entries"]:
            if entry["status"] == "deprecated" and not include_deprecated:
                continue
            score = _entry_score(query, entry)
            if score <= 0:
                continue
            rule = entry["rule"]
            summary = str(
                rule.get("summary")
                or rule.get("symptom")
                or entry["id"]
            )
            matches.append(
                {
                    "id": entry["id"],
                    "kind": kind,
                    "status": entry["status"],
                    "summary": summary,
                    "applicable_versions": entry["applicable_versions"],
                    "score": score,
                    "source_file": filename,
                }
            )
    matches.sort(key=lambda item: (-item["score"], item["kind"], item["id"]))
    return matches[:limit]


def get_knowledge_entry(
    *, knowledge_dir: Path, entry_id: str
) -> dict[str, Any] | None:
    for filename, kind in KNOWLEDGE_FILES.items():
        document = load_knowledge_file(knowledge_dir / filename)
        validate_knowledge_document(
            document, expected_kind=kind, path=str(knowledge_dir / filename)
        )
        for entry in document["entries"]:
            if entry["id"] == entry_id:
                return {"kind": kind, "source_file": filename, "entry": entry}
    return None


def write_knowledge_document(path: Path, payload: Mapping[str, Any]) -> None:
    expected_kind = KNOWLEDGE_FILES.get(path.name)
    if expected_kind is None:
        raise KnowledgeError(f"unsupported formal knowledge file: {path.name}")
    validate_knowledge_document(
        payload, expected_kind=expected_kind, path=str(path)
    )
    errors: list[str] = []
    _scan_sensitive(payload, str(path), errors)
    if errors:
        raise KnowledgeError("; ".join(errors))
    _write_json_atomic(path, payload)
