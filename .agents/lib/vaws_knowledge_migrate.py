#!/usr/bin/env python3
"""Mechanical v1 -> v2 knowledge migration.

The load-bearing v1 -> v2 change is that applicability stops being free text.
A v1 entry says things like::

    "Verified 2026-09-02 on workspace A3 containers (vllm-ascend images),
     TP2/TP4/TP8/TP16 services on <internal address range>."

Two rules govern the conversion, and they are the whole point of this module:

**Nothing is guessed.** Only tokens actually present in the source string (or
in the v1 ``scope`` object) become bounded coordinates. Every other dimension
becomes an explicit ``unresolved`` marker naming what a human must supply. A
migrated entry therefore cannot reach ``status: verified`` and cannot be
exported until somebody fills it in. Synthesising a plausible CANN or driver
range would be worse than leaving the dimension open, because it would read as
established fact.

**Nothing is claimed independent.** ``{"any": true, "basis": ...}`` is a human
claim about why a fact does not depend on a dimension. A migration script has
no basis for such a claim, so it never emits one.

**Nothing from the source string is copied through.** The v2 coordinate is
structured; the original prose (which is where addresses and hostnames live)
is not carried over at all, and the derived tokens are redaction-screened
before they are placed in an entry.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

import vaws_knowledge_v2 as v2
import vaws_redaction as redaction

# Tokens that describe an Ascend SoC / hardware generation.
_SOC_RES = (
    re.compile(r"\bAscend\s?\d{3}[A-Za-z0-9_]*\b"),
    re.compile(r"\bAtlas\s+(A\d)\b", re.IGNORECASE),
    re.compile(r"(?<![\w.-])(A[235])(?![\w.-])"),
)
_TOPOLOGY_RE = re.compile(r"(?<![\w.])((?:TP|DP|EP|PP)\s?\d{1,3})(?![\w.])", re.IGNORECASE)
_NODE_TOPOLOGY_RES = (
    (re.compile(r"\bmulti[- ]?node\b", re.IGNORECASE), "multinode"),
    (re.compile(r"\bsingle[- ]?node\b", re.IGNORECASE), "single-node"),
)
_EXECUTION_MODES = (
    (re.compile(r"\benforce[_ -]?eager\b", re.IGNORECASE), "eager"),
    (re.compile(r"\beager\b", re.IGNORECASE), "eager"),
    (re.compile(r"\bacl[_ -]?graph\b", re.IGNORECASE), "aclgraph"),
    (re.compile(r"\bpiecewise\b", re.IGNORECASE), "piecewise"),
    (
        re.compile(r"\b(?:pd|prefill[- ]decode)[- ]?disaggregat\w*\b", re.IGNORECASE),
        "prefill-decode-disaggregated",
    ),
)
# ``cann 8.2.rc1`` / ``CANN=8.2.RC1`` / ``torch 2.5.1`` style statements.
_VERSION_KEYS = {
    "cann": (r"cann",),
    "driver": (r"driver", r"hdk"),
    "python_abi": (r"python[_ -]?abi", r"soabi"),
    "torch": (r"torch(?![_ -]?npu)",),
    "torch_npu": (r"torch[_ -]?npu",),
    "vllm": (r"vllm(?![_ -]?ascend)",),
    "vllm_ascend": (r"vllm[_ -]?ascend",),
}
_VERSION_VALUE = r"(v?\d[\w.+-]*)"

_NEEDS = v2.UNRESOLVED_HINTS

# v2 ``rule`` is an exact whitelist: summary, symptom, root_cause, resolution,
# avoidance, fingerprints. A v1 document family whose rule payload is
# structured (layer counts, verified configs, capability matrices) has nowhere
# to go without flattening facts into prose, so it is reported as blocked
# instead of being lossily converted.
_NARRATIVE_KINDS = frozenset({"known-failure-signatures"})


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", str(value).strip().lower()).strip("-.")
    return slug[:128]


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _v1_scopes(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Both v1 scope locations.

    Hand-written v1 entries carry ``scope`` at entry level; entries promoted
    from a candidate carry it under ``rule.scope``. Reading only one of the two
    silently drops a coordinate the v1 author did record.
    """

    found: list[Mapping[str, Any]] = []
    for container in (entry, entry.get("rule")):
        if isinstance(container, Mapping) and isinstance(container.get("scope"), Mapping):
            found.append(container["scope"])
    return found


def _scope_text(entry: Mapping[str, Any]) -> str:
    """The only text migration is allowed to read for coordinates."""

    parts: list[str] = [str(entry.get("applicable_versions", ""))]
    for scope in _v1_scopes(entry):
        for value in scope.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(str(item) for item in value)
    return " ".join(parts)


def derive_soc(text: str) -> list[str]:
    found: list[str] = []
    for pattern in _SOC_RES:
        for match in pattern.finditer(text):
            token = (match.group(1) if match.groups() else match.group(0)).strip()
            found.append(token.upper() if len(token) <= 3 else token)
    return _dedupe(found)


def derive_topology(text: str) -> list[str]:
    found = [
        re.sub(r"\s+", "", match.group(1)).lower() for match in _TOPOLOGY_RE.finditer(text)
    ]
    for pattern, token in _NODE_TOPOLOGY_RES:
        if pattern.search(text):
            found.append(token)
    return _dedupe(found)


def derive_execution_modes(text: str) -> list[str]:
    found = [token for pattern, token in _EXECUTION_MODES if pattern.search(text)]
    return _dedupe(found)


def derive_versions(text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for dimension, keys in _VERSION_KEYS.items():
        for key in keys:
            match = re.search(
                rf"\b{key}\b\s*(?:version)?\s*[:=]?\s*{_VERSION_VALUE}",
                text,
                re.IGNORECASE,
            )
            if match:
                versions[dimension] = match.group(1)
                break
    return versions


def derive_component(entry: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for scope in _v1_scopes(entry):
        for key in ("component", "subsystem"):
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                found.append(_slugify(value))
            elif isinstance(value, list):
                found.extend(_slugify(item) for item in value if isinstance(item, str))
    return _dedupe(found)


def derive_model(entry: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for scope in _v1_scopes(entry):
        for key in ("model", "models"):
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
            elif isinstance(value, list):
                candidates.extend(str(item) for item in value)
    return _dedupe(candidates)


def derive_verified_date(text: str) -> str | None:
    match = re.search(r"\bverified\s+(\d{4}-\d{2}-\d{2})\b", text, re.IGNORECASE)
    return match.group(1) if match else None


def derive_scope(entry: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return the v2 ``scope`` plus the list of dimensions needing a human.

    Every dimension is either bounded from a token actually present in the v1
    entry, or explicitly unresolved. No ``any`` claims, no invented bounds.
    """

    text = _scope_text(entry)
    derived: dict[str, list[str]] = {
        "soc": derive_soc(text),
        "topology": derive_topology(text),
        "execution_mode": derive_execution_modes(text),
        "component": derive_component(entry),
        "model": derive_model(entry),
    }
    for dimension, version in derive_versions(text).items():
        derived[dimension] = [version]

    scope: dict[str, Any] = {}
    pending: list[dict[str, str]] = []
    for dimension in v2.SCOPE_DIMENSIONS:
        values = [value for value in derived.get(dimension, []) if value]
        # A derived token that trips redaction is dropped, never carried.
        values = [
            value
            for value in values
            if not redaction.blocking_findings(redaction.scan(value, path=dimension))
        ]
        if values:
            scope[dimension] = v2.values_constraint(values)
        else:
            needs = _NEEDS[dimension]
            scope[dimension] = v2.unresolved_constraint(needs)
            pending.append({"dimension": dimension, "needs": needs})
    return scope, pending


def _rule_from_v1(entry: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    rule = entry.get("rule")
    if not isinstance(rule, Mapping):
        return {}, ["v1 rule is not an object"]
    problems: list[str] = []
    converted: dict[str, Any] = {}
    for field in ("summary", "symptom", "root_cause", "resolution"):
        value = rule.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"v1 rule.{field} is missing; v2 requires it")
        else:
            converted[field] = value.strip()
    avoidance = rule.get("avoidance")
    if isinstance(avoidance, str) and avoidance.strip():
        converted["avoidance"] = avoidance.strip()
    fingerprints = rule.get("fingerprints")
    if isinstance(fingerprints, list):
        values = [str(item).strip() for item in fingerprints if str(item).strip()]
        if values:
            converted["fingerprints"] = values
    dropped = sorted(
        set(rule)
        - set(v2.RULE_REQUIRED)
        - set(v2.RULE_OPTIONAL)
        - {"candidate_id", "candidate_ids", "verification", "evidence", "confidence",
           "occurrence_count", "first_seen_at", "last_verified_at", "owner_skill", "scope",
           "deprecation"}
    )
    if dropped:
        problems.append(
            "v1 rule carries structured fields the v2 rule whitelist cannot hold: "
            + ", ".join(dropped)
        )
    return converted, problems


_STATUS_MAP = {"active": "unverified", "experimental": "unverified", "deprecated": "deprecated"}


def migrate_entry(
    entry: Mapping[str, Any],
    *,
    kind: str,
    origin_repo: str,
    contributor: str,
    now: str | None = None,
    existing_uuid: str | None = None,
) -> dict[str, Any]:
    """Convert one v1 entry. Returns a report record with ``entry`` or ``blocked``."""

    slug = _slugify(str(entry.get("id", "")))
    report: dict[str, Any] = {
        "v1_id": entry.get("id"),
        "kind": kind,
        "slug": slug,
        "blocked": [],
        "needs_human_input": [],
        "removed": [],
        "notes": [],
    }
    if not slug:
        report["blocked"].append("v1 entry has no usable id")
        return report
    if kind not in _NARRATIVE_KINDS:
        report["blocked"].append(
            "v2 rule accepts only summary/symptom/root_cause/resolution/avoidance/"
            "fingerprints; this document family carries structured payloads that would "
            "have to be flattened into prose"
        )

    rule, rule_problems = _rule_from_v1(entry)
    report["blocked"].extend(rule_problems)

    source_text = _scope_text(entry)
    address_findings = [
        finding.rule
        for finding in redaction.scan(source_text, path="applicable_versions")
        if finding.severity == redaction.BLOCK
    ]
    if address_findings:
        report["removed"].append(
            "v1 applicable_versions contained "
            + ", ".join(sorted(set(address_findings)))
            + "; the value is not carried into v2 and is not reproduced in this report"
        )
    if re.search(r"<redacted[^>]*>", source_text, re.IGNORECASE):
        report["removed"].append(
            "v1 applicable_versions carries a redaction placeholder; the removed value "
            "is not recoverable from the v1 document and is not reproduced here"
        )

    scope, pending = derive_scope(entry)
    report["needs_human_input"] = pending
    verified_date = derive_verified_date(source_text)
    if verified_date:
        report["notes"].append(
            f"v1 text claims verification on {verified_date}; not written to "
            "verification.last_verified_at because v2 requires a complete "
            "verified_against environment alongside it"
        )
    if isinstance(entry.get("source"), str) and entry["source"].strip():
        report["notes"].append(
            "v1 'source' prose has no v2 field; the v1 document remains readable for it"
        )

    if report["blocked"]:
        return report

    status = _STATUS_MAP.get(str(entry.get("status")), "unverified")
    confidence = str((entry.get("rule") or {}).get("confidence", "low"))
    if confidence not in v2.CONFIDENCE_LEVELS:
        confidence = "low"
    if confidence == "high":
        # high is reserved for reviewed claims; a migrated entry is not one.
        confidence = "medium"
        report["notes"].append("v1 confidence 'high' downgraded to 'medium' (not reviewed under v2)")

    first_seen = str(entry.get("first_seen") or entry.get("updated_at") or v2.today(now))
    if not v2.DATE_RE.fullmatch(first_seen):
        first_seen = v2.today(now)
        report["notes"].append("v1 first_seen was not a full date; using the migration date")
    updated_at = str(entry.get("updated_at") or v2.today(now))
    if not v2.DATE_RE.fullmatch(updated_at):
        updated_at = v2.today(now)

    migrated = {
        "uuid": existing_uuid or v2.derived_uuid(origin_repo, kind, slug),
        "slug": slug,
        "content_hash": "sha256:" + "0" * 64,
        "status": status,
        "confidence": confidence,
        "scope": scope,
        "provenance": {
            "contributor": contributor,
            "origin_repo": origin_repo,
            "submitted_at": v2.today(now),
            "redaction_profile": redaction.REDACTION_PROFILE,
        },
        "lifecycle": {
            "first_seen": first_seen,
            "updated_at": updated_at,
            "superseded_by": None,
            "resolved_by": None,
        },
        "rule": rule,
    }
    migrated = v2.with_content_hash(migrated)
    errors = v2.validate_entry(migrated, path=f"{kind}:{slug}", context=v2.PROJECT_LAYER)
    if errors:
        report["blocked"].extend(errors)
        return report
    report["entry"] = migrated
    report["status"] = status
    return report


def migrate_document(
    document: Mapping[str, Any],
    *,
    kind: str,
    origin_repo: str,
    contributor: str,
    existing: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convert a v1 document. Returns ``(v2_document, per_entry_reports)``.

    Re-running the migration is idempotent: an entry already present in the
    target document keeps its ``uuid``, so a repeated run proposes a revision
    of the same entry rather than a duplicate.
    """

    known_uuids: dict[str, str] = {}
    if isinstance(existing, Mapping):
        for entry in existing.get("entries", []):
            if isinstance(entry, Mapping) and isinstance(entry.get("slug"), str):
                known_uuids[entry["slug"]] = str(entry.get("uuid"))

    reports: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for entry in document.get("entries", []):
        if not isinstance(entry, Mapping):
            reports.append({"kind": kind, "blocked": ["entry is not an object"]})
            continue
        slug = _slugify(str(entry.get("id", "")))
        report = migrate_entry(
            entry,
            kind=kind,
            origin_repo=origin_repo,
            contributor=contributor,
            now=now,
            existing_uuid=known_uuids.get(slug),
        )
        reports.append(report)
        if "entry" in report:
            entries.append(report["entry"])

    migrated = v2.new_document(kind, layer=v2.PROJECT_LAYER, now=now)
    migrated["entries"] = sorted(entries, key=lambda item: item["slug"])
    return migrated, reports
