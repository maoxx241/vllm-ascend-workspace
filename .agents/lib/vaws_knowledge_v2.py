#!/usr/bin/env python3
"""Client-side implementation of the federated knowledge v2 contract.

Contract source of truth (not vendored here):

- ``schemas/knowledge-v2.schema.json`` — entry contract and egress whitelist
- ``docs/federation.md`` — sync keys, idempotency, ``content_hash`` canonicalization
- ``docs/lifecycle.md`` — statuses, staleness, resolution instead of deletion
- ``CONTRIBUTING.md`` — redaction rules and promotion path

in https://github.com/vllm-ascend-workspace/vaws-knowledge.

Two deliberate differences from the upstream contract, both local-only:

1. **Unresolved coordinate markers.** A migrated or freshly captured entry
   often has dimensions nobody has actually established. Upstream has no
   representation for that on purpose — an entry there is either bounded or
   explicitly claimed independent. So this fork adds a third, *project-layer
   only* constraint form ``{"unresolved": true, "needs": "..."}``. It is not
   upstream-valid, which is exactly the point: an entry carrying one cannot be
   exported and cannot reach ``status: verified``. Guessing a plausible CANN
   range instead would produce the confident-but-wrong knowledge the whole
   design exists to prevent.
2. **``layer: project``.** Project-layer documents live in this repo, not in
   the upstream corpus directories, so they carry their own layer label.
   Export rewrites it to ``unverified`` (a proposal lands in
   ``corpus/unverified/``).

Everything else — the twelve dimensions, the status/confidence rules, the
canonicalization, the ``additionalProperties: false`` egress whitelist — is
enforced exactly as documented, using the standard library only. ``jsonschema``
is not available in this environment, so validation is hand-written; it is
checked against the upstream ``examples/valid-entry.yaml`` shape in
``.agents/tests/test_knowledge_v2.py``.
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

import vaws_redaction as redaction

SCHEMA_VERSION = 2
UPSTREAM_SCHEMA_ID = (
    "https://github.com/vllm-ascend-workspace/vaws-knowledge"
    "/schemas/knowledge-v2.schema.json"
)

SCOPE_DIMENSIONS: tuple[str, ...] = (
    "soc",
    "cann",
    "driver",
    "python_abi",
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
    "model",
    "topology",
    "execution_mode",
    "component",
)
CONCRETE_REQUIRED: tuple[str, ...] = (
    "soc",
    "cann",
    "driver",
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
)
CONCRETE_OPTIONAL: tuple[str, ...] = ("python_abi", "model", "topology", "execution_mode")

ENTRY_STATUSES = frozenset({"verified", "unverified", "stale", "deprecated", "resolved"})
CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
# ``high`` is reserved for claims that survived review (docs/lifecycle.md).
HIGH_CONFIDENCE_STATUSES = frozenset({"verified", "stale", "resolved"})
# Statuses that assert the claim was actually established at least once.
EVIDENCED_STATUSES = frozenset({"verified", "stale"})
DEFAULT_RESULT_STATUSES = frozenset({"verified", "stale", "resolved"})
EVIDENCE_TYPES = frozenset({"run_manifest", "pull_request", "issue", "commit", "ci_run"})
RESOLVED_BY_TYPES = frozenset({"pull_request", "commit", "release"})
DOCUMENT_LAYERS = frozenset({"verified", "unverified", "project"})
EXPORT_LAYER = "unverified"
PROJECT_LAYER = "project"
VERIFIED_LAYER = "verified"
VERIFIED_CONTEXT = "verified"
# Reviewed lifecycle states that may exist in corpus/verified/. Unverified
# observations are the review-zone, not the shared cache.
SHARED_ENTRY_STATUSES = frozenset({"verified", "stale", "deprecated", "resolved"})

RULE_REQUIRED = ("summary", "symptom", "root_cause", "resolution")
RULE_OPTIONAL = ("avoidance", "fingerprints")
ENTRY_REQUIRED = (
    "uuid",
    "slug",
    "content_hash",
    "status",
    "confidence",
    "scope",
    "provenance",
    "lifecycle",
    "rule",
)
ENTRY_OPTIONAL = ("verification", "conflicts")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REDACTION_PROFILE_RE = re.compile(r"^r[0-9]+$")
MIN_BASIS_LENGTH = 12
MIN_NEEDS_LENGTH = 12

# What a human has to supply per dimension before an entry can leave
# ``unverified``. Shared by migration and by candidate promotion so both ask
# for the same thing in the same words.
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


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def today(now: str | None = None) -> str:
    if now:
        return now[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def new_uuid() -> str:
    return str(uuid.uuid4())


def derived_uuid(*parts: str) -> str:
    """Stable, v4-shaped uuid derived from identity parts (never from content).

    Migration and re-migration of the same v1 entry must produce the same
    identity, otherwise every migration run proposes a duplicate entry
    upstream. ``uuid.uuid5`` would be the natural fit, but the upstream schema
    pins the version nibble to ``4``, so the digest is reshaped to a v4 layout.
    The inputs are identity (origin repo, kind, slug), never entry content, so
    rewording an entry does not change its identity.
    """

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
    """Read a v2 document.

    Documents written by this fork are JSON-compatible YAML, so a missing
    PyYAML degrades to the standard library instead of failing. Documents
    pulled from upstream are block YAML and need PyYAML; that case reports a
    capability gap rather than pretending the file is empty.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeV2Error(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on environment
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
    """Validate, redaction-screen and atomically write a v2 document."""

    validate_document(document, path=str(path), context=context)
    redaction.require_writable(document, path=str(path))
    _write_json_atomic(path, document)


def new_document(kind: str, *, layer: str = PROJECT_LAYER, now: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "layer": layer,
        "updated_at": today(now),
        "entries": [],
    }


# ---------------------------------------------------------------------------
# canonicalization and content hashing (docs/federation.md)
# ---------------------------------------------------------------------------

ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_ASCII_WS_RUN = re.compile(r"[ \t\n\r\x0b\x0c]+")


def _ascii_lower(value: str) -> str:
    """Step 2: A–Z → a–z only. Non-ASCII letters are left unchanged."""

    return value.translate(_ASCII_LOWER)


def _normalize_text(value: str) -> str:
    """Step 3: LF endings, per-line trailing ASCII whitespace, then outer ASCII strip."""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip(ASCII_WHITESPACE) for line in text.split("\n"))
    return text.strip(ASCII_WHITESPACE)


def _normalize_fingerprints(items: Any) -> Any:
    """Step 2. Non-list values are left for the validator to reject."""

    if not isinstance(items, list):
        return items
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            return list(items)
        norm = _ASCII_WS_RUN.sub(" ", _ascii_lower(item).strip(ASCII_WHITESPACE))
        if norm:
            seen.add(norm)
    return sorted(seen, key=lambda value: value.encode("utf-8"))


def normalize_fingerprints(values: Sequence[Any]) -> list[str]:
    """Canonical fingerprint form per docs/federation.md (also used for dedupe)."""

    if not isinstance(values, (list, tuple)):
        raise KnowledgeV2Error(
            "fingerprints must be a list of strings; canonicalization does not "
            f"stringify {type(values).__name__}"
        )
    for index, item in enumerate(values):
        if not isinstance(item, str):
            raise KnowledgeV2Error(
                f"fingerprints[{index}] must be a string, got {type(item).__name__} "
                f"{item!r}; canonicalization does not stringify fingerprint items"
            )
    return _normalize_fingerprints(list(values))


def _payload_type_error(value: Any, path: str, *, in_fingerprints: bool = False) -> str | None:
    """Step 0: name a type that must not be coerced into a published hash."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                return (
                    f"{path}: mapping keys must be strings, got {type(key).__name__} "
                    f"{key!r}; canonicalization does not stringify keys"
                )
            child_path = f"{path}.{key}"
            if key == "fingerprints" and not isinstance(child, list):
                return (
                    f"{child_path} must be a list of strings, got {type(child).__name__}; "
                    "canonicalization does not stringify fingerprint items"
                )
            err = _payload_type_error(
                child, child_path, in_fingerprints=(key == "fingerprints")
            )
            if err:
                return err
        return None
    if isinstance(value, list):
        if in_fingerprints:
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    return (
                        f"{path}[{index}]: fingerprint items must be strings, got "
                        f"{type(item).__name__} {item!r}; canonicalization does not "
                        "stringify them"
                    )
            return None
        for index, item in enumerate(value):
            err = _payload_type_error(item, f"{path}[{index}]")
            if err:
                return err
        return None
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return None
    if isinstance(value, (int, float)):
        return (
            f"{path}: numeric value {value!r} is a {type(value).__name__} and is not "
            "canonicalized; quote it as a string (canonicalization does not stringify types)"
        )
    return (
        f"{path}: unsupported type {type(value).__name__}; "
        "canonicalization does not coerce it"
    )


def _normalize_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise KnowledgeV2Error(
                    "canonical_payload: mapping keys must be strings, got "
                    f"{type(key).__name__} {key!r}; canonicalization does not stringify keys"
                )
            out[key] = _normalize_tree(child)
        return out
    if isinstance(value, list):
        return [_normalize_tree(item) for item in value]
    if isinstance(value, str):
        return _normalize_text(value)
    return value


def canonical_object(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical ``{"rule": ..., "scope": ...}`` mapping for ``entry``."""

    if not isinstance(entry, Mapping):
        raise KnowledgeV2Error("canonical_payload: entry must be a mapping")
    missing = [name for name in ("scope", "rule") if name not in entry]
    if missing:
        raise KnowledgeV2Error(
            "canonical_payload: entry is missing "
            + ", ".join(repr(name) for name in missing)
            + "; content_hash is defined over scope + rule only and cannot be "
            "computed without both"
        )
    for key in ("scope", "rule"):
        if not isinstance(entry[key], Mapping):
            raise KnowledgeV2Error(
                f"canonical_payload: {key} must be a mapping, got "
                f"{type(entry[key]).__name__}"
            )
        err = _payload_type_error(entry[key], key)
        if err:
            raise KnowledgeV2Error("canonical_payload: " + err)
    rule = _normalize_tree(entry["rule"])
    if isinstance(rule, dict) and "fingerprints" in rule:
        # Step 2 applies to the original fingerprint strings, not to the
        # already step-3-normalized copies in the walked tree.
        rule["fingerprints"] = _normalize_fingerprints(entry["rule"].get("fingerprints"))
    scope = _normalize_tree(entry["scope"])
    return {"rule": rule, "scope": scope}


def canonical_payload(entry: Mapping[str, Any]) -> str:
    """Canonical JSON serialization of ``scope`` + ``rule``, per docs/federation.md.

    Only what the entry *claims* is hashed: not status, not dates, not
    provenance. Re-verifying or re-reviewing an entry must not change its
    revision. The return value is the exact UTF-8 JSON string that is hashed.
    """

    return json.dumps(
        canonical_object(entry),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def content_hash(entry: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_payload(entry).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def with_content_hash(entry: Mapping[str, Any]) -> dict[str, Any]:
    updated = deepcopy(dict(entry))
    updated["content_hash"] = content_hash(updated)
    return updated


# ---------------------------------------------------------------------------
# constraints
# ---------------------------------------------------------------------------


def any_constraint(basis: str) -> dict[str, Any]:
    return {"any": True, "basis": basis}


def values_constraint(values: Sequence[str]) -> dict[str, Any]:
    return {"values": list(values)}


def range_constraint(minimum: str | None, maximum: str | None) -> dict[str, Any]:
    return {"range": {"min": minimum, "max": maximum}}


def unresolved_constraint(needs: str) -> dict[str, Any]:
    """Project-layer only marker: this dimension has not been established."""

    return {"unresolved": True, "needs": needs}


def is_unresolved(constraint: Any) -> bool:
    return isinstance(constraint, Mapping) and constraint.get("unresolved") is True


def unresolved_dimensions(entry: Mapping[str, Any]) -> list[str]:
    scope = entry.get("scope")
    if not isinstance(scope, Mapping):
        return list(SCOPE_DIMENSIONS)
    return [name for name in SCOPE_DIMENSIONS if is_unresolved(scope.get(name))]


def _validate_constraint(
    value: Any, path: str, errors: list[str], *, allow_unresolved: bool
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return
    keys = set(value)
    if keys == {"any", "basis"}:
        if value.get("any") is not True:
            errors.append(f"{path}.any must be true")
        basis = value.get("basis")
        if not isinstance(basis, str) or len(basis.strip()) < MIN_BASIS_LENGTH:
            errors.append(
                f"{path}.basis must state why the fact is independent of this dimension"
            )
        return
    if keys == {"values"}:
        items = value.get("values")
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) or not item.strip() for item in items)
        ):
            errors.append(f"{path}.values must be a non-empty array of non-empty strings")
        return
    if keys == {"range"}:
        bounds = value.get("range")
        if not isinstance(bounds, Mapping) or set(bounds) != {"min", "max"}:
            errors.append(f"{path}.range must declare exactly min and max")
            return
        for side in ("min", "max"):
            bound = bounds.get(side)
            if bound is not None and not isinstance(bound, str):
                errors.append(f"{path}.range.{side} must be a string or null")
        return
    if keys in ({"unresolved"}, {"unresolved", "needs"}, {"unresolved", "needs", "note"}):
        if not allow_unresolved:
            errors.append(
                f"{path} is an unresolved project-layer marker and cannot be exported; "
                "a human must supply this coordinate first"
            )
            return
        if value.get("unresolved") is not True:
            errors.append(f"{path}.unresolved must be true")
        needs = value.get("needs")
        if not isinstance(needs, str) or len(needs.strip()) < MIN_NEEDS_LENGTH:
            errors.append(f"{path}.needs must state what a human has to supply")
        note = value.get("note")
        if note is not None and not isinstance(note, str):
            errors.append(f"{path}.note must be a string")
        return
    errors.append(
        f"{path} must be exactly one of: {{any, basis}}, {{values}}, {{range}}"
        + (", {unresolved, needs}" if allow_unresolved else "")
    )


def _validate_scope(value: Any, path: str, errors: list[str], *, allow_unresolved: bool) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return
    missing = sorted(set(SCOPE_DIMENSIONS) - set(value))
    if missing:
        errors.append(
            f"{path} must declare every dimension; missing: {', '.join(missing)}"
        )
    unknown = sorted(set(value) - set(SCOPE_DIMENSIONS))
    if unknown:
        errors.append(f"{path} has unknown dimensions: {', '.join(unknown)}")
    for name in SCOPE_DIMENSIONS:
        if name in value:
            _validate_constraint(
                value[name], f"{path}.{name}", errors, allow_unresolved=allow_unresolved
            )


def scope_summary(scope: Mapping[str, Any]) -> str:
    """Compact one-line rendering of a coordinate.

    Used to keep the v1 ``applicable_versions`` key populated for existing
    query consumers without pretending a structured coordinate is prose.
    """

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


# ---------------------------------------------------------------------------
# entry validation
# ---------------------------------------------------------------------------


def _validate_rule(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return
    for field in RULE_REQUIRED:
        text = value.get(field)
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{path}.{field} must be a non-empty string")
    avoidance = value.get("avoidance")
    if avoidance is not None and not isinstance(avoidance, str):
        errors.append(f"{path}.avoidance must be a string")
    fingerprints = value.get("fingerprints")
    if fingerprints is not None:
        if not isinstance(fingerprints, list) or any(
            not isinstance(item, str) or not item.strip() for item in fingerprints
        ):
            errors.append(f"{path}.fingerprints must be an array of non-empty strings")
    unknown = sorted(set(value) - set(RULE_REQUIRED) - set(RULE_OPTIONAL))
    if unknown:
        errors.append(
            f"{path} has fields the contract does not declare: {', '.join(unknown)}"
        )


def _validate_provenance(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return
    for field in ("contributor", "origin_repo"):
        text = value.get(field)
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{path}.{field} must be a non-empty string")
    submitted_at = value.get("submitted_at")
    if not isinstance(submitted_at, str) or not DATE_RE.fullmatch(submitted_at):
        errors.append(f"{path}.submitted_at must use YYYY-MM-DD")
    profile = value.get("redaction_profile")
    if not isinstance(profile, str) or not REDACTION_PROFILE_RE.fullmatch(profile):
        errors.append(f"{path}.redaction_profile must look like r1")
    unknown = sorted(
        set(value) - {"contributor", "origin_repo", "submitted_at", "redaction_profile"}
    )
    if unknown:
        errors.append(f"{path} has unknown fields: {', '.join(unknown)}")


def _validate_concrete_environment(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return
    for field in CONCRETE_REQUIRED:
        text = value.get(field)
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{path}.{field} must be the exact observed value")
    for field in CONCRETE_OPTIONAL:
        if field in value and not isinstance(value[field], str):
            errors.append(f"{path}.{field} must be a string")
    unknown = sorted(set(value) - set(CONCRETE_REQUIRED) - set(CONCRETE_OPTIONAL))
    if unknown:
        errors.append(f"{path} has unknown fields: {', '.join(unknown)}")


def _validate_verification(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return
    evidence = value.get("evidence")
    if not isinstance(evidence, list):
        errors.append(f"{path}.evidence must be an array")
    else:
        for index, item in enumerate(evidence):
            item_path = f"{path}.evidence[{index}]"
            if not isinstance(item, Mapping):
                errors.append(f"{item_path} must be an object")
                continue
            if item.get("type") not in EVIDENCE_TYPES:
                errors.append(
                    f"{item_path}.type must be one of: {', '.join(sorted(EVIDENCE_TYPES))}"
                )
            ref = item.get("ref")
            if not isinstance(ref, str) or not ref.strip():
                errors.append(f"{item_path}.ref must be a followable reference")
            note = item.get("note")
            if note is not None and not isinstance(note, str):
                errors.append(f"{item_path}.note must be a string")
            unknown = sorted(set(item) - {"type", "ref", "note"})
            if unknown:
                errors.append(f"{item_path} has unknown fields: {', '.join(unknown)}")
    verified_by = value.get("verified_by")
    if not isinstance(verified_by, list) or any(
        not isinstance(item, str) or not item.strip() for item in verified_by
    ):
        errors.append(f"{path}.verified_by must be an array of non-empty strings")
    _validate_concrete_environment(
        value.get("verified_against"), f"{path}.verified_against", errors
    )
    last_verified_at = value.get("last_verified_at")
    if not isinstance(last_verified_at, str) or not DATE_RE.fullmatch(last_verified_at):
        errors.append(f"{path}.last_verified_at must use YYYY-MM-DD")
    unknown = sorted(
        set(value) - {"evidence", "verified_by", "verified_against", "last_verified_at"}
    )
    if unknown:
        errors.append(f"{path} has unknown fields: {', '.join(unknown)}")


def _validate_lifecycle(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return
    for field in ("first_seen", "updated_at"):
        text = value.get(field)
        if not isinstance(text, str) or not DATE_RE.fullmatch(text):
            errors.append(f"{path}.{field} must use YYYY-MM-DD")
    supersedes = value.get("supersedes")
    if supersedes is not None:
        if not isinstance(supersedes, list) or any(
            not isinstance(item, str) or not UUID_RE.fullmatch(item) for item in supersedes
        ):
            errors.append(f"{path}.supersedes must be an array of entry uuids")
    superseded_by = value.get("superseded_by")
    if superseded_by is not None and not (
        isinstance(superseded_by, str) and UUID_RE.fullmatch(superseded_by)
    ):
        errors.append(f"{path}.superseded_by must be an entry uuid or null")
    resolved_by = value.get("resolved_by")
    if resolved_by is not None:
        if not isinstance(resolved_by, Mapping):
            errors.append(f"{path}.resolved_by must be an object or null")
        else:
            if resolved_by.get("type") not in RESOLVED_BY_TYPES:
                errors.append(
                    f"{path}.resolved_by.type must be one of: "
                    + ", ".join(sorted(RESOLVED_BY_TYPES))
                )
            ref = resolved_by.get("ref")
            if not isinstance(ref, str) or not ref.strip():
                errors.append(f"{path}.resolved_by.ref must be a non-empty reference")
            unknown = sorted(set(resolved_by) - {"type", "ref"})
            if unknown:
                errors.append(
                    f"{path}.resolved_by has unknown fields: {', '.join(unknown)}"
                )
    unknown = sorted(
        set(value) - {"first_seen", "updated_at", "supersedes", "superseded_by", "resolved_by"}
    )
    if unknown:
        errors.append(f"{path} has unknown fields: {', '.join(unknown)}")


def _validate_conflicts(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{path} must be an array")
        return
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{item_path} must be an object")
            continue
        with_uuid = item.get("with")
        if not isinstance(with_uuid, str) or not UUID_RE.fullmatch(with_uuid):
            errors.append(f"{item_path}.with must be an entry uuid")
        dimensions = item.get("undeclared_dimensions")
        if (
            not isinstance(dimensions, list)
            or not dimensions
            or any(name not in SCOPE_DIMENSIONS for name in dimensions)
        ):
            errors.append(
                f"{item_path}.undeclared_dimensions must name at least one scope dimension"
            )
        recorded_at = item.get("recorded_at")
        if not isinstance(recorded_at, str) or not DATE_RE.fullmatch(recorded_at):
            errors.append(f"{item_path}.recorded_at must use YYYY-MM-DD")
        note = item.get("note")
        if note is not None and not isinstance(note, str):
            errors.append(f"{item_path}.note must be a string")
        unknown = sorted(set(item) - {"with", "undeclared_dimensions", "recorded_at", "note"})
        if unknown:
            errors.append(f"{item_path} has unknown fields: {', '.join(unknown)}")


def validate_entry(
    entry: Mapping[str, Any],
    *,
    path: str = "entry",
    context: str = PROJECT_LAYER,
    check_hash: bool = True,
) -> list[str]:
    """Return contract violations for one entry.

    ``context='project'`` allows unresolved coordinate markers.
    ``context='export'`` refuses them, which is the whole point of the marker.
    ``context='verified'`` is the shared-cache boundary: unresolved markers
    and unverified entries are refused.
    """

    allow_unresolved = context == PROJECT_LAYER
    errors: list[str] = []
    if not isinstance(entry, Mapping):
        return [f"{path} must be an object"]

    entry_uuid = entry.get("uuid")
    if not isinstance(entry_uuid, str) or not UUID_RE.fullmatch(entry_uuid):
        errors.append(f"{path}.uuid must be a v4-shaped uuid")
    slug = entry.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        errors.append(f"{path}.slug must be a lowercase safe handle")
    declared_hash = entry.get("content_hash")
    if not isinstance(declared_hash, str) or not CONTENT_HASH_RE.fullmatch(declared_hash):
        errors.append(f"{path}.content_hash must look like sha256:<64 hex>")

    status = entry.get("status")
    if status not in ENTRY_STATUSES:
        errors.append(f"{path}.status must be one of: {', '.join(sorted(ENTRY_STATUSES))}")
    elif context == VERIFIED_CONTEXT and status not in SHARED_ENTRY_STATUSES:
        errors.append(
            f"{path}.status {status} cannot enter the shared verified zone"
        )
    confidence = entry.get("confidence")
    if confidence not in CONFIDENCE_LEVELS:
        errors.append(
            f"{path}.confidence must be one of: {', '.join(sorted(CONFIDENCE_LEVELS))}"
        )
    if confidence == "high" and status not in HIGH_CONFIDENCE_STATUSES:
        errors.append(
            f"{path}.confidence high is only allowed on verified/stale/resolved entries"
        )

    _validate_scope(entry.get("scope"), f"{path}.scope", errors, allow_unresolved=allow_unresolved)
    _validate_provenance(entry.get("provenance"), f"{path}.provenance", errors)
    _validate_lifecycle(entry.get("lifecycle"), f"{path}.lifecycle", errors)
    _validate_rule(entry.get("rule"), f"{path}.rule", errors)
    if "verification" in entry:
        _validate_verification(entry["verification"], f"{path}.verification", errors)
    if "conflicts" in entry:
        _validate_conflicts(entry["conflicts"], f"{path}.conflicts", errors)

    if status in EVIDENCED_STATUSES:
        verification = entry.get("verification")
        if not isinstance(verification, Mapping):
            errors.append(f"{path}.verification is required for status {status}")
        else:
            if not verification.get("evidence"):
                errors.append(
                    f"{path}.verification.evidence needs at least one followable reference"
                )
            if not verification.get("verified_by"):
                errors.append(
                    f"{path}.verification.verified_by needs at least one confirming handle"
                )
    if status == "resolved":
        lifecycle = entry.get("lifecycle")
        resolved_by = lifecycle.get("resolved_by") if isinstance(lifecycle, Mapping) else None
        if not isinstance(resolved_by, Mapping):
            errors.append(
                f"{path}.lifecycle.resolved_by must point at the fix for a resolved entry"
            )

    pending = unresolved_dimensions(entry)
    if pending and status not in {"unverified", "deprecated"}:
        errors.append(
            f"{path} still has unresolved dimensions ({', '.join(pending)}); "
            f"status {status} requires a complete coordinate"
        )

    unknown = sorted(set(entry) - set(ENTRY_REQUIRED) - set(ENTRY_OPTIONAL))
    if unknown:
        errors.append(
            f"{path} has fields the egress whitelist does not declare: {', '.join(unknown)}"
        )
    missing = sorted(set(ENTRY_REQUIRED) - set(entry))
    if missing:
        errors.append(f"{path} is missing required fields: {', '.join(missing)}")

    if check_hash and isinstance(entry.get("scope"), Mapping) and isinstance(entry.get("rule"), Mapping):
        try:
            expected = content_hash(entry)
        except KnowledgeV2Error as exc:
            errors.append(f"{path}: {exc}")
            expected = None
        if expected is not None and declared_hash != expected:
            errors.append(
                f"{path}.content_hash does not match the canonicalized scope+rule payload"
            )
    return errors


def validate_document(
    document: Mapping[str, Any],
    *,
    path: str = "document",
    expected_kind: str | None = None,
    context: str = PROJECT_LAYER,
) -> None:
    errors: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    kind = document.get("kind")
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
        errors.append("kind must be a lowercase document family name")
    elif expected_kind is not None and kind != expected_kind:
        errors.append(f"kind must be {expected_kind!r}")
    layer = document.get("layer")
    if layer not in DOCUMENT_LAYERS:
        errors.append(f"layer must be one of: {', '.join(sorted(DOCUMENT_LAYERS))}")
    if context == "export" and layer != EXPORT_LAYER:
        errors.append(f"exported documents must declare layer {EXPORT_LAYER!r}")
    if context == VERIFIED_CONTEXT and layer != VERIFIED_LAYER:
        errors.append(
            f"shared verified-zone documents must declare layer {VERIFIED_LAYER!r}, "
            f"not {layer!r}"
        )
    updated_at = document.get("updated_at")
    if not isinstance(updated_at, str) or not DATE_RE.fullmatch(updated_at):
        errors.append("updated_at must use YYYY-MM-DD")
    entries = document.get("entries")
    if not isinstance(entries, list):
        errors.append("entries must be an array")
        entries = []
    seen_uuids: set[str] = set()
    seen_slugs: set[str] = set()
    for index, entry in enumerate(entries):
        errors.extend(validate_entry(entry, path=f"entries[{index}]", context=context))
        if isinstance(entry, Mapping):
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
    unknown = sorted(set(document) - {"schema_version", "kind", "layer", "updated_at", "entries"})
    if unknown:
        errors.append(f"unknown top-level fields: {', '.join(unknown)}")
    if errors:
        raise KnowledgeV2Error(f"{path}: " + "; ".join(errors))


# ---------------------------------------------------------------------------
# reading a directory of v2 documents
# ---------------------------------------------------------------------------


def document_kind_from_name(name: str) -> str | None:
    if not name.endswith(V2_SUFFIX):
        return None
    return name[: -len(V2_SUFFIX)]


def iter_documents(root: Path) -> list[tuple[Path, str]]:
    """Return ``(path, kind)`` for every v2 document in ``root``."""

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
    """Load every v2 entry under ``root``.

    Returns ``(entries, problems)``. A malformed document degrades to a
    reported problem instead of an exception: a knowledge lookup must never be
    the reason a diagnosis stops.
    """

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


def match_view(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt a v2 entry to the shape the v1 scorer understands."""

    return {
        "id": entry.get("slug", entry.get("uuid", "")),
        "status": entry.get("status"),
        "rule": entry.get("rule", {}),
        "applicable_versions": scope_summary(entry.get("scope", {})),
    }


def is_default_result(entry: Mapping[str, Any]) -> bool:
    """Default result set is verified + stale + resolved (docs/lifecycle.md)."""

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
    if status == "deprecated":
        return "deprecated: the claim was wrong or superseded"
    pending = unresolved_dimensions(entry)
    if pending:
        return "unresolved coordinate: " + ", ".join(pending)
    return None


# ---------------------------------------------------------------------------
# export path
# ---------------------------------------------------------------------------


def export_entry(
    entry: Mapping[str, Any],
    *,
    contributor: str,
    origin_repo: str,
    submitted_at: str | None = None,
) -> dict[str, Any]:
    """Return an upstream-shaped entry, or raise with the exact blocker.

    Order matters: unresolved coordinates and redaction findings are refused
    before anything is serialized, so a blocked entry never reaches a file
    that could be committed or pushed.
    """

    prepared = deepcopy(dict(entry))
    prepared.pop("_kind", None)
    prepared.pop("_source_file", None)
    pending = unresolved_dimensions(prepared)
    if pending:
        raise KnowledgeV2Error(
            "cannot export an entry with unresolved dimensions: " + ", ".join(pending)
        )
    prepared["provenance"] = {
        "contributor": contributor,
        "origin_repo": origin_repo,
        "submitted_at": submitted_at or today(),
        "redaction_profile": redaction.REDACTION_PROFILE,
    }
    prepared["content_hash"] = content_hash(prepared)
    errors = validate_entry(prepared, path="entry", context="export")
    if errors:
        raise KnowledgeV2Error("; ".join(errors))
    redaction.require_exportable(prepared, path="entry")
    return prepared


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
    """Serialize for an upstream proposal: block YAML when possible.

    JSON is valid YAML, so a missing PyYAML degrades to a JSON body rather
    than blocking the export. The upstream repo re-serializes canonically
    anyway.
    """

    try:
        import yaml
    except ImportError:  # pragma: no cover - depends on environment
        return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return yaml.safe_dump(
        json.loads(json.dumps(document)),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
