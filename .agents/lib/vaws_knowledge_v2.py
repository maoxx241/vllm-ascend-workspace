#!/usr/bin/env python3
"""Project-layer I/O and curation over the installed ``vaws-knowledge`` engine.

Hashing, schema, body validation, redaction, export whitelist, and
measurement-contradiction rules live in the package. This module keeps
document paths, unresolved-coordinate hints for curators, and the local
write/load workflow.
"""
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
from typing import Any, Mapping, Sequence

from vaws_knowledge import canonical, export as commons_export, redact, validate as commons_validate
from vaws_knowledge._common import ToolError
from vaws_knowledge.canonical import BODY_KEYS
from vaws_knowledge.canonical import body_key as commons_body_key
from vaws_knowledge.canonical import content_hash as commons_content_hash

RUNTIME_BODY_KEYS = tuple(key for key in BODY_KEYS if key != "reference")
from vaws_knowledge.server.capture import EVIDENCE_TYPES as CAPTURE_EVIDENCE_TYPES
from vaws_knowledge.server.capture import schema_validate, validate_entry as commons_validate_entry
from vaws_knowledge.server.query import SCOPE_DIMENSIONS, searchable_view
from vaws_knowledge.validate import CONCRETE_ENV_FIELDS, SCOPE_DIMENSIONS as VALIDATE_SCOPE_DIMENSIONS

import vaws_redaction as redaction

_CANONICAL_ERRORS = (ValueError, ToolError)

SCHEMA_VERSION = 2
SCOPE_DIMENSIONS = tuple(VALIDATE_SCOPE_DIMENSIONS or SCOPE_DIMENSIONS)
CONCRETE_REQUIRED: tuple[str, ...] = (
    "soc",
    "cann",
    "driver",
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
)
CONCRETE_OPTIONAL: tuple[str, ...] = tuple(
    name for name in CONCRETE_ENV_FIELDS if name not in CONCRETE_REQUIRED
)
ENTRY_STATUSES = frozenset({"verified", "unverified", "stale", "deprecated", "resolved"})
CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
DEFAULT_RESULT_STATUSES = frozenset({"verified", "stale", "resolved"})
EVIDENCE_TYPES = frozenset(CAPTURE_EVIDENCE_TYPES)
DOCUMENT_LAYERS = frozenset({"verified", "unverified", "project"})
EXPORT_LAYER = "unverified"
PROJECT_LAYER = "project"
VERIFIED_LAYER = "verified"
VERIFIED_CONTEXT = "verified"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UNRESOLVED_HINTS: dict[str, str] = {
    "soc": "the exact SoC the fix was observed on (e.g. Ascend910_93)",
    "cann": "the CANN version of the container the fix was verified on",
    "driver": "the NPU driver / firmware version of the verification host",
    "python_abi": "the Python ABI tag of the verification container",
    "torch": "the torch version installed at verification time",
    "torch_npu": "the torch_npu version installed at verification time",
    "vllm": "the vllm version (or commit) used at verification time",
    "vllm_ascend": "the vllm-ascend version (or commit) used at verification time",
    "model": "the model(s) this claim was established on, or an examined independence basis",
    "topology": "the parallel topologies this claim was established on",
    "execution_mode": "the execution mode(s) this claim was established on",
    "component": "the owning subsystem (e.g. service-bootstrap, ssh-transport, hccl)",
}
V2_SUFFIX = ".v2.yaml"


class KnowledgeV2Error(ValueError):
    """Raised when a document or entry violates the federated v2 contract."""


def today(now: str | None = None) -> str:
    if now:
        return now[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def new_uuid() -> str:
    return str(uuid.uuid4())


def derived_uuid(*parts: str) -> str:
    """Stable, v4-shaped uuid derived from identity parts (never from content)."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    raw = bytearray(digest[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


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


def yaml_available() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def load_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeV2Error(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise KnowledgeV2Error(
                f"{path} is block YAML and PyYAML is unavailable: {exc}"
            ) from exc
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise KnowledgeV2Error(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise KnowledgeV2Error(f"{path}: document root must be an object")
    return payload


def write_document(path: Path, document: Mapping[str, Any], *, context: str = PROJECT_LAYER) -> None:
    validate_document(document, path=str(path), context=context)
    redaction.require_writable(document, path=str(path))
    _write_json_atomic(path, document)


def new_document(kind: str, *, layer: str = EXPORT_LAYER, now: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "layer": layer,
        "updated_at": today(now),
        "entries": [],
    }


def normalize_fingerprints(values: Sequence[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        text = re.sub(r"[ \t\n\r\x0b\x0c]+", " ", item.strip().translate(str.maketrans(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
        )))
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return sorted(out, key=lambda value: value.encode("utf-8"))


def body_key(entry: Mapping[str, Any]) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    return commons_body_key(entry)


def with_content_hash(entry: Mapping[str, Any]) -> dict[str, Any]:
    updated = deepcopy(dict(entry))
    try:
        updated["content_hash"] = commons_content_hash(updated)
    except _CANONICAL_ERRORS as exc:
        raise KnowledgeV2Error(str(exc)) from exc
    return updated


def any_constraint(basis: str) -> dict[str, Any]:
    return {"any": True, "basis": basis}


def values_constraint(values: Sequence[str]) -> dict[str, Any]:
    return {"values": list(values)}


def range_constraint(minimum: str | None, maximum: str | None) -> dict[str, Any]:
    return {"range": {"min": minimum, "max": maximum}}


def is_unresolved(constraint: Any) -> bool:
    if not isinstance(constraint, Mapping):
        return False
    if constraint.get("unresolved") is True:
        return True
    bounds = constraint.get("range")
    return (
        isinstance(bounds, Mapping)
        and bounds.get("min") is None
        and bounds.get("max") is None
    )


def unresolved_needs(dimension: str) -> str:
    return UNRESOLVED_HINTS[dimension]


def unresolved_dimensions(entry: Mapping[str, Any]) -> list[str]:
    if body_key(entry) == "reference":
        return []
    scope = entry.get("scope")
    if not isinstance(scope, Mapping):
        return list(SCOPE_DIMENSIONS)
    return [name for name in SCOPE_DIMENSIONS if is_unresolved(scope.get(name))]


def scope_summary(scope: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for name in SCOPE_DIMENSIONS:
        constraint = scope.get(name) if isinstance(scope, Mapping) else None
        if not isinstance(constraint, Mapping):
            parts.append(f"{name}=?")
        elif is_unresolved(constraint):
            parts.append(f"{name}=UNRESOLVED")
        elif constraint.get("any") is True:
            parts.append(f"{name}=any")
        elif isinstance(constraint.get("values"), list):
            parts.append(f"{name}=" + ",".join(str(item) for item in constraint["values"]))
        elif isinstance(constraint.get("range"), Mapping):
            bounds = constraint["range"]
            low = bounds.get("min") or "*"
            high = bounds.get("max") or "*"
            parts.append(f"{name}={low}..{high}")
        else:
            parts.append(f"{name}=?")
    return "; ".join(parts)


def _prefix_problems(path: str, problems: Sequence[str]) -> list[str]:
    out: list[str] = []
    for problem in problems:
        text = str(problem)
        if text.startswith(path):
            out.append(text)
        else:
            out.append(f"{path}: {text}" if path != "entry" else text)
    return out


def validate_entry(
    entry: Mapping[str, Any],
    *,
    path: str = "entry",
    context: str = PROJECT_LAYER,
    check_hash: bool = True,
) -> list[str]:
    """Return package contract violations plus project-layer coordinate gates."""
    if not isinstance(entry, Mapping):
        return [f"{path} must be an object"]
    kind = str(entry.get("_kind") or "workspace")
    if not KIND_RE.fullmatch(kind):
        kind = "workspace"
    payload = {key: value for key, value in entry.items() if not str(key).startswith("_")}
    errors = list(commons_validate_entry(payload, kind=kind))
    body = body_key(payload)
    pending = unresolved_dimensions(payload)
    status = payload.get("status")
    if body in RUNTIME_BODY_KEYS and pending and status not in {"unverified", "deprecated"}:
        errors.append(
            f"still has unresolved dimensions ({', '.join(pending)}); "
            f"status {status} requires a complete coordinate"
        )
    if context in {"export", VERIFIED_CONTEXT} and pending:
        errors.append(
            "cannot export or verify with unresolved dimensions: " + ", ".join(pending)
        )
    if context == VERIFIED_CONTEXT:
        wrapper = {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "layer": VERIFIED_LAYER,
            "updated_at": today(),
            "entries": [payload],
        }
        problems, _count = commons_validate.validate_document(wrapper, path)
        errors.extend(problem.message for problem in problems)
        if status == "unverified":
            errors.append("status unverified cannot enter the shared verified zone")
    if check_hash:
        try:
            expected = commons_content_hash(payload)
        except _CANONICAL_ERRORS as exc:
            errors.append(str(exc))
            expected = None
        declared = payload.get("content_hash")
        if expected is not None and declared != expected:
            errors.append(
                f"content_hash does not match the canonicalized "
                f"{'reference' if body == 'reference' else 'scope+' + str(body)} payload"
            )
    return _prefix_problems(path, errors)


def validate_document(
    document: Mapping[str, Any],
    *,
    path: str = "document",
    expected_kind: str | None = None,
    context: str = PROJECT_LAYER,
) -> None:
    errors: list[str] = []
    if not isinstance(document, Mapping):
        raise KnowledgeV2Error(f"{path} must be an object")
    kind = document.get("kind")
    if expected_kind is not None and kind != expected_kind:
        errors.append(f"kind must be {expected_kind!r}")
    layer = document.get("layer")
    if context == "export" and layer not in {EXPORT_LAYER, PROJECT_LAYER}:
        errors.append(f"exported documents must declare layer {EXPORT_LAYER!r}")
    if context == VERIFIED_CONTEXT and layer != VERIFIED_LAYER:
        errors.append(f"shared verified-zone documents must declare layer {VERIFIED_LAYER!r}")
    problems, _count = commons_validate.validate_document(document, path)
    errors.extend(problem.render() for problem in problems)
    entries = document.get("entries") if isinstance(document.get("entries"), list) else []
    seen_uuids: set[str] = set()
    seen_slugs: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        tagged = dict(entry)
        tagged["_kind"] = kind if isinstance(kind, str) else "workspace"
        errors.extend(validate_entry(tagged, path=f"entries[{index}]", context=context))
        entry_uuid = entry.get("uuid")
        if isinstance(entry_uuid, str):
            if entry_uuid in seen_uuids:
                errors.append(f"entries[{index}].uuid is duplicated: {entry_uuid}")
            seen_uuids.add(entry_uuid)
        slug = entry.get("slug")
        if isinstance(slug, str):
            if slug in seen_slugs:
                errors.append(f"entries[{index}].slug is duplicated: {slug}")
            seen_slugs.add(slug)
    schema = schema_validate(document)
    if schema.get("ran") and schema.get("errors"):
        errors.extend(str(item) for item in schema["errors"])
    if errors:
        raise KnowledgeV2Error(f"{path}: " + "; ".join(errors))


def document_kind_from_name(name: str) -> str | None:
    if not name.endswith(V2_SUFFIX):
        return None
    return name[: -len(V2_SUFFIX)]


def iter_documents(root: Path) -> list[tuple[Path, str]]:
    if not root.is_dir():
        return []
    found: list[tuple[Path, str]] = []
    for path in sorted(root.glob(f"*{V2_SUFFIX}")):
        kind = document_kind_from_name(path.name)
        if kind:
            found.append((path, kind))
    return found


def load_entries(
    root: Path, *, context: str = PROJECT_LAYER, validate: bool = True
) -> tuple[list[dict[str, Any]], list[str]]:
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for path, kind in iter_documents(root):
        try:
            document = load_document(path)
            if validate:
                validate_document(
                    document, path=str(path), expected_kind=kind, context=context
                )
        except KnowledgeV2Error as exc:
            problems.append(str(exc))
            continue
        for entry in document.get("entries", []):
            if not isinstance(entry, Mapping):
                problems.append(f"{path}: entry is not an object")
                continue
            record = deepcopy(dict(entry))
            record["_kind"] = kind
            record["_source_file"] = path.name
            entries.append(record)
    return entries, problems


def entry_summary(entry: Mapping[str, Any]) -> str:
    body = body_key(entry)
    payload = entry.get(body) if body else None
    if isinstance(payload, Mapping):
        summary = payload.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
        if body == "rule":
            symptom = payload.get("symptom")
            if isinstance(symptom, str) and symptom.strip():
                return symptom
    return str(entry.get("slug") or entry.get("uuid") or "")


def match_view(entry: Mapping[str, Any]) -> dict[str, Any]:
    body = body_key(entry) or "rule"
    view = searchable_view(entry)
    rule = entry.get("rule") if isinstance(entry.get("rule"), Mapping) else {}
    measurement = (
        entry.get("measurement") if isinstance(entry.get("measurement"), Mapping) else {}
    )
    return {
        "id": entry.get("slug", entry.get("uuid", "")),
        "status": entry.get("status"),
        "body": body,
        "rule": {
            "summary": view.get("summary") if body != "rule" else rule.get("summary"),
            "symptom": None if body != "rule" else rule.get("symptom"),
            "root_cause": None if body != "rule" else rule.get("root_cause"),
            "resolution": view.get("resolution") if body != "rule" else rule.get("resolution"),
            "fingerprints": view.get("fingerprints")
            if body != "rule"
            else rule.get("fingerprints", []),
        },
        "measurement": measurement if body == "measurement" else None,
        "applicable_versions": scope_summary(entry.get("scope", {}) if isinstance(entry.get("scope"), Mapping) else {}),
    }


def is_default_result(entry: Mapping[str, Any]) -> bool:
    return entry.get("status") in DEFAULT_RESULT_STATUSES


def status_warning(entry: Mapping[str, Any]) -> str | None:
    status = entry.get("status")
    if status == "stale":
        return "stale: was verified, but not re-verified against a recent environment"
    if status == "resolved":
        lifecycle = entry.get("lifecycle", {})
        resolved_by = lifecycle.get("resolved_by") if isinstance(lifecycle, Mapping) else None
        if isinstance(resolved_by, Mapping):
            return f"resolved by {resolved_by.get('type')}:{resolved_by.get('ref')}"
        return "resolved"
    if status == "unverified":
        return "unverified: single observation, nobody else confirmed it"
    return None


def _as_entry_ref(entry: Mapping[str, Any], *, index: int = 0):
    from vaws_knowledge.bot.corpus import EntryRef

    return EntryRef(
        path="<memory>",
        doc_index=0,
        entry_index=index,
        kind=str(entry.get("_kind") or "workspace"),
        layer="project",
        entry=entry,
    )


def measurement_contradictions(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Delegate to ``vaws_knowledge.bot.conflicts.measurement_contradiction``."""
    from vaws_knowledge.bot.conflicts import diff_coordinates, measurement_contradiction

    a = _as_entry_ref(left, index=0)
    b = _as_entry_ref(right, index=1)
    if not diff_coordinates(a, b).overlaps:
        return []
    return list(measurement_contradiction(a, b) or [])


def document_has_measurement_conflict(document: Mapping[str, Any]) -> bool:
    entries = document.get("entries")
    if not isinstance(entries, list):
        return False
    live = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("status") in {"verified", "unverified", "stale"}
    ]
    for index, left in enumerate(live):
        for right in live[index + 1 :]:
            if measurement_contradictions(left, right):
                return True
    return False


def export_entry(
    entry: Mapping[str, Any],
    *,
    contributor: str,
    origin_repo: str,
    submitted_at: str | None = None,
) -> dict[str, Any]:
    prepared = deepcopy(dict(entry))
    prepared.pop("_kind", None)
    prepared.pop("_source_file", None)
    body = body_key(prepared)
    pending = unresolved_dimensions(prepared)
    if pending:
        raise KnowledgeV2Error(
            "cannot export an entry with unresolved dimensions: " + ", ".join(pending)
        )
    if body == "reference":
        prepared.pop("scope", None)
        prepared.pop("verification", None)
    prepared["provenance"] = {
        "contributor": contributor,
        "origin_repo": origin_repo,
        "submitted_at": submitted_at or today(),
        "redaction_profile": redact.REDACTION_PROFILE,
    }
    try:
        prepared["content_hash"] = commons_content_hash(prepared)
    except _CANONICAL_ERRORS as exc:
        raise KnowledgeV2Error(str(exc)) from exc
    errors = validate_entry(prepared, path="entry", context="export")
    if errors:
        raise KnowledgeV2Error("; ".join(errors))
    findings = redact.scan_tree(prepared)
    if findings:
        raise KnowledgeV2Error(
            "redaction found values that must not be published: "
            + "; ".join(item.render() for item in findings)
        )
    return commons_export.order_entry(prepared)


def export_document(
    kind: str,
    entries: Sequence[Mapping[str, Any]],
    *,
    contributor: str,
    origin_repo: str,
    submitted_at: str | None = None,
) -> dict[str, Any]:
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "layer": EXPORT_LAYER,
        "updated_at": submitted_at or today(),
        "entries": [
            export_entry(
                entry,
                contributor=contributor,
                origin_repo=origin_repo,
                submitted_at=submitted_at,
            )
            for entry in entries
        ],
    }
    validate_document(document, path=f"{kind}{V2_SUFFIX}", expected_kind=kind, context="export")
    return document


def serialize_document(document: Mapping[str, Any]) -> str:
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return yaml.safe_dump(
        json.loads(json.dumps(document)),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
