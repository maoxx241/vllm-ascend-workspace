#!/usr/bin/env python3
"""Shared Result Envelope v1: the agent-facing diagnosability contract.

Every workspace entry point should return one envelope on ``stdout``, in
success and in failure, so that an agent can locate the failing layer
without re-running the operation.

Design notes live in ``docs/agent-feedback-contract.md``. The machine
readable contract is ``.agents/schemas/result-envelope-v1.schema.json``;
this module is the authoritative validator because ``jsonschema`` is not
guaranteed to be installed on a client machine.

Two serialization modes exist on purpose:

* ``dumps(envelope)`` keeps full runtime fidelity (hosts, ports, absolute
  paths) and is only ever safe to write under untracked ``.vaws-local/``.
* ``dumps_publishable(envelope)`` redacts first and refuses to emit if a
  leak pattern survives redaction. Use it for anything that may end up in
  a tracked file, a PR body, or a knowledge candidate.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

SCHEMA_VERSION = "vaws.result-envelope.v1"
SCHEMA_MAJOR = 1
ACCEPTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})

PROGRESS_SENTINEL = "__VAWS_PROGRESS__="

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

OUTCOMES = frozenset({"success", "partial", "failure", "blocked", "cancelled"})
FAILING_OUTCOMES = frozenset({"partial", "failure", "blocked"})

#: The failure taxonomy. The point of the taxonomy is that a confident wrong
#: attribution costs more than an honest ``unknown``, so ``unknown`` is a
#: first-class member and every other member requires stated evidence.
LAYERS = (
    "caller",
    "tool",
    "transport",
    "remote_env",
    "remote_workload",
    "device",
    "unknown",
)
LAYER_SET = frozenset(LAYERS)

LAYER_DESCRIPTIONS: Mapping[str, str] = {
    "caller": "the invocation was wrong (bad arguments, missing target, "
    "unsupported flag combination); nothing was attempted downstream",
    "tool": "the local wrapper, shared library, or tool service itself "
    "misbehaved (crash, non-JSON output, instant timeout from the tool "
    "service rather than from the remote command)",
    "transport": "SSH, network, port forwarding, or connection multiplexing "
    "failed before the remote command produced a result",
    "remote_env": "the remote container environment is wrong (missing path, "
    "import error, version mismatch, missing hostname mapping)",
    "remote_workload": "the command under test ran and failed on its own "
    "terms; this is the only layer that says 'the code under test is guilty'",
    "device": "NPU or another exclusive resource was unavailable, busy, or "
    "the lease was denied",
    "unknown": "the evidence does not identify a layer; say so instead of "
    "guessing, and state in next_step what would narrow it",
}

CONFIDENCES = frozenset({"high", "medium", "low"})
TARGET_KINDS = frozenset(
    {"local", "host", "container", "session", "fleet", "service", "unknown"}
)
ENVIRONMENT_SOURCES = frozenset(
    {"probe", "manifest", "declared", "cache", "unknown"}
)
IDEMPOTENCY_CLASSES = frozenset(
    {"idempotent", "at_most_once", "unsafe_to_retry", "unknown"}
)
PART_OUTCOMES = frozenset({"success", "failure", "blocked", "skipped"})

#: Environment identity at the granularity the knowledge base compares on.
ENVIRONMENT_FIELDS = (
    "soc",
    "cann",
    "driver",
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
)

#: Well-known reason codes. The validator only enforces the shape, so skills
#: may add their own; reuse these when they fit so that fingerprints stay
#: comparable across skills.
REASON_CODES: Mapping[str, str] = {
    "bad_arguments": "caller",
    "missing_target": "caller",
    "consent_required": "caller",
    "wrapper_crash": "tool",
    "non_json_output": "tool",
    "tool_service_instant_timeout": "tool",
    "ssh_connect_failed": "transport",
    "ssh_mux_stream_died": "transport",
    "remote_timeout": "transport",
    "import_error": "remote_env",
    "version_mismatch": "remote_env",
    "path_missing": "remote_env",
    "hostname_unresolvable": "remote_env",
    "workload_nonzero_exit": "remote_workload",
    "workload_assertion_failed": "remote_workload",
    "service_not_ready": "remote_workload",
    "npu_busy": "device",
    "lease_denied": "device",
    "unattributed": "unknown",
}

REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
ENVELOPE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

TOP_LEVEL_FIELDS = (
    "schema_version",
    "envelope_id",
    "emitted_at",
    "operation",
    "outcome",
    "exit_code",
    "summary",
    "attempt",
    "failure",
    "environment",
    "evidence",
    "next_step",
    "parts",
    "attempts",
    "children",
    "warnings",
    "extensions",
)
TOP_LEVEL_SET = frozenset(TOP_LEVEL_FIELDS)

# ---------------------------------------------------------------------------
# Bounded output (same head/tail preview shape as .remote-dev/core/preview.py)
# ---------------------------------------------------------------------------

DEFAULT_HEAD_CHARS = 4000
DEFAULT_TAIL_CHARS = 4000
MAX_SUMMARY_CHARS = 400


class EnvelopeError(ValueError):
    """Raised when an envelope violates the v1 contract."""


class EnvelopeRedactionError(EnvelopeError):
    """Raised when a publishable serialization would still leak identity."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def new_envelope_id(prefix: str = "env") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    token = re.sub(r"[^a-z0-9]+", "-", prefix.lower()).strip("-") or "env"
    return f"{token}-{stamp}-{uuid.uuid4().hex[:8]}"


def text_preview(
    value: str,
    *,
    ref: str | None = None,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> dict[str, Any]:
    """Bounded text preview with a pointer to the full content.

    Field names intentionally match ``.remote-dev/core/preview.py`` so that a
    remote-dev result can be lifted into an envelope without reshaping. The
    added ``ref`` is what makes truncation safe: a truncated preview without
    a ref is rejected by :func:`validate_envelope`.
    """
    byte_count = len(value.encode("utf-8", errors="replace"))
    payload: dict[str, Any] = {
        "bytes": byte_count,
        "head_chars": head_chars,
        "tail_chars": tail_chars,
        "ref": ref,
    }
    if len(value) <= head_chars + tail_chars:
        payload["text"] = value
        payload["truncated"] = False
        return payload
    payload["head"] = value[:head_chars]
    payload["tail"] = value[-tail_chars:]
    payload["truncated"] = True
    return payload


def evidence_ref(
    *,
    name: str,
    kind: str,
    ref: str,
    bytes_: int | None = None,
    sha256: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """One evidence pointer. ``ref`` is a locator, never inlined content."""
    return {
        "name": name,
        "kind": kind,
        "ref": ref,
        "bytes": bytes_,
        "sha256": sha256,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

#: Only literals that identify nothing survive redaction. RFC 5737
#: documentation ranges are deliberately *not* on this list: a redactor that
#: has to reason about whether an address is "safe" is a redactor that will
#: eventually be wrong, and loopback is the only address a diagnosis actually
#: needs to keep.
_SAFE_IPV4 = (
    re.compile(r"^127\."),
    re.compile(r"^0\.0\.0\.0$"),
    re.compile(r"^255\.255\.255\.255$"),
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b")
_USER_AT_HOST_RE = re.compile(r"\b[\w.+-]{1,64}@[\w.-]{1,255}\b")
_HOME_PATH_RE = re.compile(r"(?:/Users|/home)/[^/\s\"']+")
_WINDOWS_HOME_RE = re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\s\"']+")
#: Shared data roots that live under ``/home`` on Ascend hosts but name no
#: user. Model paths are load-bearing diagnostic information — losing them
#: would make two results incomparable for the sake of hiding nothing.
_SAFE_HOME_PREFIXES = (
    "/home/weights",
    "/home/models",
    "/home/data",
    "/home/cache",
    "/home/shared",
)
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|auth|credential|pass(?:word)?|secret|token)"
    r"(?:_|$)",
    re.IGNORECASE,
)
_SECRET_VALUE_RES = (
    re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_SAFE_HOST_SUFFIXES = (".invalid", ".example", ".example.com", ".localhost")

REDACTED_HOST = "<redacted-host>"
REDACTED_USER = "<redacted-user>"
REDACTED_SECRET = "<redacted-secret>"
REDACTED_HOME = "<redacted-home>"


def _ipv4_is_safe(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SAFE_IPV4)


def _home_path_is_safe(value: str) -> bool:
    return value.startswith(_SAFE_HOME_PREFIXES)


def _host_is_safe(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"localhost", "example.invalid"}:
        return True
    return any(lowered.endswith(suffix) for suffix in _SAFE_HOST_SUFFIXES)


def redact_text(value: str) -> str:
    """Replace runtime identity with stable placeholders.

    Loopback addresses and ``*.invalid`` / ``*.example`` hostnames survive:
    they identify nothing and a diagnosis is much harder to read without
    them. Everything else that looks like an address, a ``user@host`` pair, a
    home directory or a credential is replaced.
    """
    result = value
    for pattern in _SECRET_VALUE_RES:
        result = pattern.sub(REDACTED_SECRET, result)

    def _mask_user_at_host(match: re.Match[str]) -> str:
        text = match.group(0)
        user, _, host = text.partition("@")
        if _host_is_safe(host) or (
            _IPV4_RE.fullmatch(host) and _ipv4_is_safe(host)
        ):
            return f"{REDACTED_USER}@{host}"
        return f"{REDACTED_USER}@{REDACTED_HOST}"

    result = _USER_AT_HOST_RE.sub(_mask_user_at_host, result)
    result = _IPV4_RE.sub(
        lambda m: m.group(0) if _ipv4_is_safe(m.group(0)) else REDACTED_HOST,
        result,
    )
    result = _IPV6_RE.sub(
        lambda m: m.group(0) if m.group(0) in {"::1"} else REDACTED_HOST,
        result,
    )
    result = _HOME_PATH_RE.sub(
        lambda m: m.group(0) if _home_path_is_safe(m.group(0)) else REDACTED_HOME,
        result,
    )
    result = _WINDOWS_HOME_RE.sub(REDACTED_HOME, result)
    return result


def _redact_value(key: str | None, value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, str):
        if key is not None and _SECRET_KEY_RE.search(key):
            return REDACTED_SECRET
        return redact_text(value)
    return value


def redact(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy with runtime identity replaced by placeholders."""
    return _redact_value(None, dict(envelope))


def leak_findings(payload: Any, *, path: str = "$") -> list[str]:
    """Report residual identity leaks as ``<json path>: <reason>`` strings."""
    findings: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            child = f"{path}.{key}"
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                if value not in (None, REDACTED_SECRET):
                    findings.append(f"{child}: secret-like key carries a value")
            findings.extend(leak_findings(value, path=child))
        return findings
    if isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            findings.extend(leak_findings(value, path=f"{path}[{index}]"))
        return findings
    if isinstance(payload, str):
        for candidate in _IPV4_RE.findall(payload):
            if not _ipv4_is_safe(candidate):
                findings.append(f"{path}: routable IPv4 literal")
                break
        for candidate in _IPV6_RE.findall(payload):
            if candidate != "::1":
                findings.append(f"{path}: IPv6 literal")
                break
        for candidate in _HOME_PATH_RE.findall(payload):
            if not _home_path_is_safe(candidate):
                findings.append(f"{path}: absolute user home path")
                break
        if _WINDOWS_HOME_RE.search(payload):
            findings.append(f"{path}: absolute user home path")
        for pattern in _SECRET_VALUE_RES:
            if pattern.search(payload):
                findings.append(f"{path}: secret-like value")
                break
        for match in _USER_AT_HOST_RE.finditer(payload):
            _, _, host = match.group(0).partition("@")
            if not _host_is_safe(host) and not (
                _IPV4_RE.fullmatch(host) and _ipv4_is_safe(host)
            ):
                findings.append(f"{path}: user@host pair")
                break
    return findings


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def make_operation(
    *,
    entry_point: str,
    action: str,
    skill: str | None = None,
    target_kind: str = "unknown",
    target_id: str | None = None,
    target_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "skill": skill,
        "entry_point": entry_point,
        "action": action,
        "target": {"kind": target_kind, "id": target_id, "ref": target_ref},
    }


def make_command(
    *,
    argv: Sequence[str],
    cwd: str | None = None,
    env_keys: Sequence[str] | None = None,
    timeout_seconds: float | None = None,
    display: str | None = None,
) -> dict[str, Any]:
    argv_list = [str(part) for part in argv]
    return {
        "argv": argv_list,
        "display": display or shlex.join(argv_list),
        "cwd": cwd,
        "env_keys": sorted({str(key) for key in (env_keys or ())}),
        "timeout_seconds": timeout_seconds,
    }


def make_remote_command(
    *,
    endpoint_kind: str,
    endpoint_ref: str | None = None,
    argv: Sequence[str] | None = None,
    script: str | None = None,
    script_ref: str | None = None,
    cwd: str | None = None,
    env_keys: Sequence[str] | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """The command as it actually ran on the far side of the transport.

    Local ``argv`` alone is not reproducible for remote work: the agent needs
    the exact remote script, bounded, plus a ref to the full text.
    """
    if endpoint_kind not in TARGET_KINDS:
        raise EnvelopeError(f"unsupported endpoint kind: {endpoint_kind!r}")
    return {
        "endpoint": {"kind": endpoint_kind, "ref": endpoint_ref},
        "argv": [str(part) for part in argv] if argv is not None else None,
        "script_preview": (
            text_preview(script, ref=script_ref) if script is not None else None
        ),
        "script_ref": script_ref,
        "cwd": cwd,
        "env_keys": sorted({str(key) for key in (env_keys or ())}),
        "timeout_seconds": timeout_seconds,
    }


def make_attempt(
    *,
    command: Mapping[str, Any],
    reproduce: str,
    remote_command: Mapping[str, Any] | None = None,
    started_at: str | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "command": dict(command),
        "remote_command": dict(remote_command) if remote_command else None,
        "reproduce": reproduce,
        "started_at": started_at or utc_now(),
        "duration_ms": duration_ms,
    }


def make_failure(
    *,
    layer: str,
    reason_code: str,
    message: str,
    attribution_basis: Sequence[str] | None = None,
    confidence: str = "medium",
    ruled_out: Sequence[str] | None = None,
    signals: Sequence[Mapping[str, Any]] | None = None,
    exception_type: str | None = None,
) -> dict[str, Any]:
    """Build the failure block.

    ``attribution_basis`` is mandatory for every layer except ``unknown``:
    naming a layer is a claim, and the claim must cite the observation that
    supports it. This is what keeps ``unknown`` honest rather than a dumping
    ground for whatever the traceback happened to say.
    """
    return {
        "layer": layer,
        "confidence": confidence,
        "reason_code": reason_code,
        "message": message,
        "attribution_basis": [str(item) for item in (attribution_basis or ())],
        "ruled_out": [str(item) for item in (ruled_out or ())],
        "signals": [dict(signal) for signal in (signals or ())],
        "exception_type": exception_type,
    }


def unknown_failure(
    *,
    message: str,
    reason_code: str = "unattributed",
    ruled_out: Sequence[str] | None = None,
    signals: Sequence[Mapping[str, Any]] | None = None,
    exception_type: str | None = None,
) -> dict[str, Any]:
    """An honest unknown: no layer claim, low confidence, nothing invented."""
    return make_failure(
        layer="unknown",
        reason_code=reason_code,
        message=message,
        confidence="low",
        ruled_out=ruled_out,
        signals=signals,
        exception_type=exception_type,
    )


def make_environment(
    *,
    source: str = "unknown",
    captured_at: str | None = None,
    **versions: str | None,
) -> dict[str, Any]:
    """Environment identity at knowledge-base granularity.

    All seven fields are always present. An explicit ``null`` means "not
    captured"; a missing key would be indistinguishable from a producer that
    never knew the field existed.
    """
    if source not in ENVIRONMENT_SOURCES:
        raise EnvelopeError(f"unsupported environment source: {source!r}")
    unknown_keys = sorted(set(versions) - set(ENVIRONMENT_FIELDS))
    if unknown_keys:
        raise EnvelopeError(
            f"unsupported environment fields: {', '.join(unknown_keys)}"
        )
    payload: dict[str, Any] = {
        field: versions.get(field) for field in ENVIRONMENT_FIELDS
    }
    payload["source"] = source
    payload["captured_at"] = captured_at
    payload["unknown_fields"] = sorted(
        field for field in ENVIRONMENT_FIELDS if payload[field] is None
    )
    return payload


def make_evidence(
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    manifest_ref: str | None = None,
    refs: Sequence[Mapping[str, Any]] | None = None,
    previews: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "manifest_ref": manifest_ref,
        "refs": [dict(ref) for ref in (refs or ())],
        "previews": {
            str(name): dict(preview)
            for name, preview in (previews or {}).items()
        },
    }


def make_next_step(
    *,
    actions: Sequence[Mapping[str, Any] | str] | None = None,
    do_not: Sequence[str] | None = None,
    knowledge: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """What to look at next, and explicitly what not to look at.

    ``do_not`` carries as much value as ``actions``: the recorded gloo
    ``/etc/hosts`` signature is mostly worth having because it tells you not
    to start tuning HCCL environment variables.
    """
    normalized: list[dict[str, Any]] = []
    for action in actions or ():
        if isinstance(action, str):
            normalized.append({"description": action, "command": None, "ref": None})
            continue
        normalized.append(
            {
                "description": str(action.get("description", "")),
                "command": action.get("command"),
                "ref": action.get("ref"),
            }
        )
    return {
        "actions": normalized,
        "do_not": [str(item) for item in (do_not or ())],
        "knowledge": [dict(item) for item in (knowledge or ())],
    }


def knowledge_reference(
    *,
    entry_id: str,
    summary: str,
    avoidance: str | None = None,
    score: int | None = None,
) -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "summary": summary,
        "avoidance": avoidance,
        "score": score,
    }


def make_part(
    *,
    unit: str,
    outcome: str,
    unit_kind: str = "node",
    layer: str | None = None,
    reason_code: str | None = None,
    summary: str | None = None,
    refs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """One unit of a fan-out operation.

    A four-node operation where three nodes succeeded is not a boolean, and
    collapsing it to one loses exactly the information needed to decide
    whether the failure is systemic or node-local.
    """
    return {
        "unit": unit,
        "unit_kind": unit_kind,
        "outcome": outcome,
        "layer": layer,
        "reason_code": reason_code,
        "summary": summary,
        "refs": [dict(ref) for ref in (refs or ())],
    }


def make_attempts(
    *,
    count: int = 1,
    records: Sequence[Mapping[str, Any]] | None = None,
    idempotency_class: str = "unknown",
    side_effects: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Retry history plus whether re-running is safe.

    ``retry_safe`` is derived from ``idempotency_class`` rather than set by
    the caller, so the two can never disagree, and it stays ``null`` unless
    the producer actually classified the operation.
    """
    if idempotency_class not in IDEMPOTENCY_CLASSES:
        raise EnvelopeError(
            f"unsupported idempotency class: {idempotency_class!r}"
        )
    retry_safe: bool | None = None
    if idempotency_class == "idempotent":
        retry_safe = True
    elif idempotency_class == "unsafe_to_retry":
        retry_safe = False
    return {
        "count": count,
        "records": [dict(record) for record in (records or ())],
        "idempotency": {
            "class": idempotency_class,
            "retry_safe": retry_safe,
            "side_effects": [str(item) for item in (side_effects or ())],
        },
    }


def attempt_record(
    *,
    index: int,
    outcome: str,
    layer: str | None = None,
    reason_code: str | None = None,
    duration_ms: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "index": index,
        "outcome": outcome,
        "layer": layer,
        "reason_code": reason_code,
        "duration_ms": duration_ms,
        "note": note,
    }


def new_envelope(
    *,
    operation: Mapping[str, Any],
    outcome: str,
    summary: str,
    attempt: Mapping[str, Any],
    environment: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    next_step: Mapping[str, Any] | None = None,
    failure: Mapping[str, Any] | None = None,
    parts: Sequence[Mapping[str, Any]] | None = None,
    attempts: Mapping[str, Any] | None = None,
    children: Sequence[Mapping[str, Any]] | None = None,
    warnings: Sequence[str] | None = None,
    extensions: Mapping[str, Any] | None = None,
    exit_code: int | None = None,
    envelope_id: str | None = None,
    emitted_at: str | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "envelope_id": envelope_id or new_envelope_id(
            str(operation.get("action") or "op")
        ),
        "emitted_at": emitted_at or utc_now(),
        "operation": dict(operation),
        "outcome": outcome,
        "exit_code": exit_code if exit_code is not None else default_exit_code(outcome),
        "summary": summary,
        "attempt": dict(attempt),
        "failure": dict(failure) if failure else None,
        "environment": dict(environment) if environment else make_environment(),
        "evidence": dict(evidence) if evidence else make_evidence(),
        "next_step": dict(next_step) if next_step else make_next_step(),
        "parts": [dict(part) for part in (parts or ())],
        "attempts": dict(attempts) if attempts else make_attempts(),
        "children": [dict(child) for child in (children or ())],
        "warnings": [str(item) for item in (warnings or ())],
        "extensions": dict(extensions or {}),
    }
    if validate:
        validate_envelope(envelope)
    return envelope


def default_exit_code(outcome: str) -> int:
    """Map an outcome to the conventional process exit code.

    Distinct codes let a shell caller branch without parsing JSON, while the
    envelope stays the source of truth for anything finer.
    """
    return {
        "success": 0,
        "partial": 1,
        "failure": 1,
        "blocked": 2,
        "cancelled": 3,
    }.get(outcome, 1)


# ---------------------------------------------------------------------------
# Partial success and composition
# ---------------------------------------------------------------------------


def outcome_from_parts(parts: Sequence[Mapping[str, Any]]) -> str:
    """Derive an aggregate outcome from per-unit results."""
    if not parts:
        return "success"
    outcomes = [str(part.get("outcome")) for part in parts]
    failed = [item for item in outcomes if item in {"failure", "blocked"}]
    succeeded = [item for item in outcomes if item == "success"]
    if not failed:
        return "success"
    if not succeeded:
        return "blocked" if all(item == "blocked" for item in failed) else "failure"
    return "partial"


def failure_from_parts(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Summarize failing units into one top-level failure block.

    A single shared layer across every failing unit is reported with that
    layer; a mix is reported as ``unknown``, because "some nodes hit the
    transport and some hit the workload" is genuinely not one diagnosis.
    """
    failing = [
        part
        for part in parts
        if str(part.get("outcome")) in {"failure", "blocked"}
    ]
    if not failing:
        return None
    layers = {part.get("layer") for part in failing}
    total = len(parts)
    units = ", ".join(str(part.get("unit")) for part in failing[:8])
    basis = [f"{len(failing)} of {total} units failed: {units}"]
    if len(layers) == 1 and None not in layers:
        layer = next(iter(layers))
        codes = {
            part.get("reason_code") for part in failing if part.get("reason_code")
        }
        return make_failure(
            layer=str(layer),
            reason_code=str(next(iter(codes))) if len(codes) == 1 else "unattributed",
            message=f"{len(failing)}/{total} units failed in layer {layer}",
            attribution_basis=basis + [f"all failing units attributed to {layer}"],
            confidence="medium" if len(failing) < total else "high",
        )
    return unknown_failure(
        message=(
            f"{len(failing)}/{total} units failed with mixed or missing layer "
            "attribution"
        ),
        signals=[{"kind": "part_layers", "value": sorted(str(x) for x in layers)}],
    )


def child_digest(
    child: Mapping[str, Any],
    *,
    ref: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """Collapse a nested envelope into a bounded digest.

    Nesting whole envelopes would let a three-level call chain crowd out the
    diagnosis it exists to deliver, so the parent keeps identity, outcome and
    attribution, and points at the full child through ``ref``.
    """
    failure = child.get("failure") or {}
    operation = child.get("operation") or {}
    return {
        "envelope_id": child.get("envelope_id"),
        "entry_point": operation.get("entry_point"),
        "action": operation.get("action"),
        "outcome": child.get("outcome"),
        "layer": failure.get("layer"),
        "reason_code": failure.get("reason_code"),
        "summary": child.get("summary"),
        "ref": ref,
        "depth": depth,
    }


def escalate_child_layer(child_layer: str) -> str:
    """Map a nested call's layer onto the parent's own layer.

    A nested ``caller`` fault does not stay ``caller`` at the parent: the
    parent *is* the caller, so the parent built bad arguments and the parent's
    fault is ``tool``. Every other layer propagates unchanged, because the
    parent adds no information about it.
    """
    if child_layer not in LAYER_SET:
        return "unknown"
    return "tool" if child_layer == "caller" else child_layer


def compose_child(
    parent: MutableMapping[str, Any],
    child: Mapping[str, Any],
    *,
    ref: str | None = None,
    adopt_failure: bool = True,
    validate: bool = True,
) -> dict[str, Any]:
    """Attach a nested envelope to its parent and propagate attribution.

    The parent keeps its own summary and command, adopts the child's layer
    through :func:`escalate_child_layer` when it has no failure of its own,
    and inherits the child's ``do_not`` guidance so a known signature is not
    lost one frame up the stack.
    """
    composed = deepcopy(dict(parent))
    depth = 1 + max(
        (int(item.get("depth") or 1) for item in child.get("children") or ()),
        default=0,
    )
    composed["children"] = list(composed.get("children") or ()) + [
        child_digest(child, ref=ref, depth=depth)
    ]
    child_failure = child.get("failure")
    if adopt_failure and child_failure and not composed.get("failure"):
        child_layer = str(child_failure.get("layer") or "unknown")
        layer = escalate_child_layer(child_layer)
        basis = [
            f"nested call {child.get('operation', {}).get('entry_point')} "
            f"reported layer {child_layer}"
        ]
        if layer == "tool" and child_layer == "caller":
            basis.append(
                "a nested caller fault is this wrapper's fault: it built the "
                "arguments"
            )
        else:
            basis.extend(child_failure.get("attribution_basis") or ())
        composed["failure"] = make_failure(
            layer=layer,
            reason_code=str(child_failure.get("reason_code") or "unattributed"),
            message=str(child_failure.get("message") or "nested call failed"),
            attribution_basis=basis,
            confidence=str(child_failure.get("confidence") or "low")
            if layer != "unknown"
            else "low",
            ruled_out=child_failure.get("ruled_out") or (),
            signals=child_failure.get("signals") or (),
        )
        child_outcome = str(child.get("outcome") or "failure")
        if composed.get("outcome") == "success":
            composed["outcome"] = (
                child_outcome if child_outcome in FAILING_OUTCOMES else "failure"
            )
            composed["exit_code"] = default_exit_code(composed["outcome"])
        child_next = child.get("next_step") or {}
        parent_next = composed.get("next_step") or make_next_step()
        merged_do_not = list(
            dict.fromkeys(
                list(parent_next.get("do_not") or ())
                + list(child_next.get("do_not") or ())
            )
        )
        parent_next["do_not"] = merged_do_not
        if not parent_next.get("actions"):
            parent_next["actions"] = list(child_next.get("actions") or ())
        if not parent_next.get("knowledge"):
            parent_next["knowledge"] = list(child_next.get("knowledge") or ())
        composed["next_step"] = parent_next
    if validate:
        validate_envelope(composed)
    return composed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require_str(value: Any, path: str, errors: list[str], *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        errors.append(f"{path} must be a non-empty string")


def _require_opt_str(value: Any, path: str, errors: list[str]) -> None:
    if value is not None and not isinstance(value, str):
        errors.append(f"{path} must be a string or null")


def _require_str_list(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        errors.append(f"{path} must be an array of strings")


def _validate_operation(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("operation must be an object")
        return
    _require_str(value.get("entry_point"), "operation.entry_point", errors)
    entry_point = value.get("entry_point")
    if isinstance(entry_point, str) and entry_point.startswith("/"):
        errors.append(
            "operation.entry_point must be repo-relative, not an absolute path"
        )
    _require_str(value.get("action"), "operation.action", errors)
    _require_opt_str(value.get("skill"), "operation.skill", errors)
    target = value.get("target")
    if not isinstance(target, Mapping):
        errors.append("operation.target must be an object")
        return
    if target.get("kind") not in TARGET_KINDS:
        errors.append(
            "operation.target.kind must be one of: "
            + ", ".join(sorted(TARGET_KINDS))
        )
    _require_opt_str(target.get("id"), "operation.target.id", errors)
    _require_opt_str(target.get("ref"), "operation.target.ref", errors)


def _validate_command(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return
    _require_str_list(value.get("argv"), f"{path}.argv", errors)
    if isinstance(value.get("argv"), list) and not value["argv"]:
        errors.append(f"{path}.argv must not be empty")
    _require_str(value.get("display"), f"{path}.display", errors)
    _require_opt_str(value.get("cwd"), f"{path}.cwd", errors)
    _require_str_list(value.get("env_keys"), f"{path}.env_keys", errors)


def _validate_preview(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be a preview object")
        return
    if not isinstance(value.get("truncated"), bool):
        errors.append(f"{path}.truncated must be a boolean")
        return
    if value["truncated"]:
        if not isinstance(value.get("ref"), str) or not value["ref"].strip():
            errors.append(
                f"{path} is truncated and must carry a ref to the full content"
            )
        if "head" not in value or "tail" not in value:
            errors.append(f"{path} is truncated and must carry head and tail")
    elif "text" not in value:
        errors.append(f"{path} is not truncated and must carry text")


def _validate_attempt(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("attempt must be an object")
        return
    _validate_command(value.get("command"), "attempt.command", errors)
    _require_str(value.get("reproduce"), "attempt.reproduce", errors)
    timestamp = value.get("started_at")
    if not isinstance(timestamp, str) or not RFC3339_UTC_RE.fullmatch(timestamp):
        errors.append("attempt.started_at must be an RFC3339 UTC timestamp")
    duration = value.get("duration_ms")
    if duration is not None and not isinstance(duration, int):
        errors.append("attempt.duration_ms must be an integer or null")
    remote = value.get("remote_command")
    if remote is None:
        return
    if not isinstance(remote, Mapping):
        errors.append("attempt.remote_command must be an object or null")
        return
    endpoint = remote.get("endpoint")
    if not isinstance(endpoint, Mapping) or endpoint.get("kind") not in TARGET_KINDS:
        errors.append("attempt.remote_command.endpoint.kind must be a target kind")
    if remote.get("argv") is None and remote.get("script_preview") is None:
        errors.append(
            "attempt.remote_command must carry argv or script_preview so the "
            "remote step is reproducible by hand"
        )
    if remote.get("argv") is not None:
        _require_str_list(remote.get("argv"), "attempt.remote_command.argv", errors)
    if remote.get("script_preview") is not None:
        _validate_preview(
            remote.get("script_preview"),
            "attempt.remote_command.script_preview",
            errors,
        )


def _validate_failure(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("failure must be an object or null")
        return
    layer = value.get("layer")
    if layer not in LAYER_SET:
        errors.append("failure.layer must be one of: " + ", ".join(LAYERS))
    if value.get("confidence") not in CONFIDENCES:
        errors.append(
            "failure.confidence must be one of: " + ", ".join(sorted(CONFIDENCES))
        )
    reason_code = value.get("reason_code")
    if not isinstance(reason_code, str) or not REASON_CODE_RE.fullmatch(reason_code):
        errors.append(f"failure.reason_code must match {REASON_CODE_RE.pattern}")
    _require_str(value.get("message"), "failure.message", errors)
    _require_str_list(
        value.get("attribution_basis"), "failure.attribution_basis", errors
    )
    _require_str_list(value.get("ruled_out"), "failure.ruled_out", errors)
    basis = value.get("attribution_basis")
    if layer != "unknown" and isinstance(basis, list) and not basis:
        errors.append(
            "failure.attribution_basis must be non-empty when a layer is "
            "claimed; use layer 'unknown' instead of guessing"
        )
    if layer == "unknown" and value.get("confidence") == "high":
        errors.append("failure.confidence cannot be high for layer 'unknown'")
    ruled_out = value.get("ruled_out")
    if isinstance(ruled_out, list):
        unsupported = sorted(set(ruled_out) - LAYER_SET)
        if unsupported:
            errors.append(
                "failure.ruled_out contains unknown layers: "
                + ", ".join(unsupported)
            )
        if layer in ruled_out:
            errors.append("failure.ruled_out must not contain failure.layer")
    signals = value.get("signals")
    if not isinstance(signals, list) or any(
        not isinstance(item, Mapping) for item in signals
    ):
        errors.append("failure.signals must be an array of objects")
    _require_opt_str(value.get("exception_type"), "failure.exception_type", errors)


def _validate_environment(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("environment must be an object")
        return
    for field in ENVIRONMENT_FIELDS:
        if field not in value:
            errors.append(
                f"environment.{field} must be present (explicit null when not "
                "captured)"
            )
            continue
        _require_opt_str(value.get(field), f"environment.{field}", errors)
    if value.get("source") not in ENVIRONMENT_SOURCES:
        errors.append(
            "environment.source must be one of: "
            + ", ".join(sorted(ENVIRONMENT_SOURCES))
        )
    _require_opt_str(value.get("captured_at"), "environment.captured_at", errors)
    _require_str_list(
        value.get("unknown_fields"), "environment.unknown_fields", errors
    )
    expected = sorted(
        field for field in ENVIRONMENT_FIELDS if value.get(field) is None
    )
    if isinstance(value.get("unknown_fields"), list) and sorted(
        value["unknown_fields"]
    ) != expected:
        errors.append(
            "environment.unknown_fields must list exactly the null version "
            f"fields: {expected}"
        )


def _validate_evidence(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("evidence must be an object")
        return
    for field in ("run_id", "parent_run_id", "manifest_ref"):
        _require_opt_str(value.get(field), f"evidence.{field}", errors)
    refs = value.get("refs")
    if not isinstance(refs, list):
        errors.append("evidence.refs must be an array")
    else:
        for index, ref in enumerate(refs):
            path = f"evidence.refs[{index}]"
            if not isinstance(ref, Mapping):
                errors.append(f"{path} must be an object")
                continue
            for field in ("name", "kind", "ref"):
                _require_str(ref.get(field), f"{path}.{field}", errors)
    previews = value.get("previews")
    if not isinstance(previews, Mapping):
        errors.append("evidence.previews must be an object")
        return
    for name, preview in previews.items():
        _validate_preview(preview, f"evidence.previews.{name}", errors)


def _validate_next_step(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("next_step must be an object")
        return
    actions = value.get("actions")
    if not isinstance(actions, list):
        errors.append("next_step.actions must be an array")
    else:
        for index, action in enumerate(actions):
            path = f"next_step.actions[{index}]"
            if not isinstance(action, Mapping):
                errors.append(f"{path} must be an object")
                continue
            _require_str(action.get("description"), f"{path}.description", errors)
            _require_opt_str(action.get("command"), f"{path}.command", errors)
            _require_opt_str(action.get("ref"), f"{path}.ref", errors)
    _require_str_list(value.get("do_not"), "next_step.do_not", errors)
    knowledge = value.get("knowledge")
    if not isinstance(knowledge, list):
        errors.append("next_step.knowledge must be an array")
        return
    for index, item in enumerate(knowledge):
        path = f"next_step.knowledge[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{path} must be an object")
            continue
        _require_str(item.get("entry_id"), f"{path}.entry_id", errors)
        _require_str(item.get("summary"), f"{path}.summary", errors)


def _validate_parts(value: Any, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append("parts must be an array")
        return
    seen: set[str] = set()
    for index, part in enumerate(value):
        path = f"parts[{index}]"
        if not isinstance(part, Mapping):
            errors.append(f"{path} must be an object")
            continue
        _require_str(part.get("unit"), f"{path}.unit", errors)
        unit = part.get("unit")
        if isinstance(unit, str):
            if unit in seen:
                errors.append(f"{path}.unit is duplicated: {unit!r}")
            seen.add(unit)
        if part.get("outcome") not in PART_OUTCOMES:
            errors.append(
                f"{path}.outcome must be one of: " + ", ".join(sorted(PART_OUTCOMES))
            )
        layer = part.get("layer")
        if layer is not None and layer not in LAYER_SET:
            errors.append(f"{path}.layer must be a layer or null")
        if part.get("outcome") in {"failure", "blocked"} and layer is None:
            errors.append(
                f"{path} failed and must carry a layer (use 'unknown' when the "
                "evidence does not identify one)"
            )


def _validate_attempts(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("attempts must be an object")
        return
    count = value.get("count")
    if not isinstance(count, int) or count < 1:
        errors.append("attempts.count must be an integer >= 1")
    records = value.get("records")
    if not isinstance(records, list):
        errors.append("attempts.records must be an array")
    elif isinstance(count, int) and len(records) > count:
        errors.append("attempts.records must not be longer than attempts.count")
    idempotency = value.get("idempotency")
    if not isinstance(idempotency, Mapping):
        errors.append("attempts.idempotency must be an object")
        return
    klass = idempotency.get("class")
    if klass not in IDEMPOTENCY_CLASSES:
        errors.append(
            "attempts.idempotency.class must be one of: "
            + ", ".join(sorted(IDEMPOTENCY_CLASSES))
        )
    expected = {"idempotent": True, "unsafe_to_retry": False}.get(str(klass))
    if idempotency.get("retry_safe") != expected:
        errors.append(
            "attempts.idempotency.retry_safe must be derived from class "
            f"(expected {expected!r})"
        )
    _require_str_list(
        idempotency.get("side_effects"),
        "attempts.idempotency.side_effects",
        errors,
    )


def _validate_children(value: Any, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append("children must be an array")
        return
    for index, child in enumerate(value):
        path = f"children[{index}]"
        if not isinstance(child, Mapping):
            errors.append(f"{path} must be an object")
            continue
        _require_str(child.get("envelope_id"), f"{path}.envelope_id", errors)
        if child.get("outcome") not in OUTCOMES:
            errors.append(f"{path}.outcome must be an envelope outcome")
        layer = child.get("layer")
        if layer is not None and layer not in LAYER_SET:
            errors.append(f"{path}.layer must be a layer or null")
        depth = child.get("depth")
        if not isinstance(depth, int) or depth < 1:
            errors.append(f"{path}.depth must be an integer >= 1")
        if isinstance(child, Mapping) and "children" in child:
            errors.append(
                f"{path} must be a digest, not a nested envelope; point at the "
                "full child through ref"
            )


def validate_envelope(envelope: Mapping[str, Any]) -> None:
    """Raise :class:`EnvelopeError` describing every contract violation."""
    errors: list[str] = []
    if not isinstance(envelope, Mapping):
        raise EnvelopeError("envelope root must be an object")
    if envelope.get("schema_version") not in ACCEPTED_SCHEMA_VERSIONS:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    envelope_id = envelope.get("envelope_id")
    if not isinstance(envelope_id, str) or not ENVELOPE_ID_RE.fullmatch(envelope_id):
        errors.append(f"envelope_id must match {ENVELOPE_ID_RE.pattern}")
    emitted_at = envelope.get("emitted_at")
    if not isinstance(emitted_at, str) or not RFC3339_UTC_RE.fullmatch(emitted_at):
        errors.append("emitted_at must be an RFC3339 UTC timestamp ending in Z")
    outcome = envelope.get("outcome")
    if outcome not in OUTCOMES:
        errors.append("outcome must be one of: " + ", ".join(sorted(OUTCOMES)))
    exit_code = envelope.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        errors.append("exit_code must be an integer or null")
    summary = envelope.get("summary")
    _require_str(summary, "summary", errors)
    if isinstance(summary, str) and len(summary) > MAX_SUMMARY_CHARS:
        errors.append(f"summary must be at most {MAX_SUMMARY_CHARS} characters")

    _validate_operation(envelope.get("operation"), errors)
    _validate_attempt(envelope.get("attempt"), errors)
    _validate_environment(envelope.get("environment"), errors)
    _validate_evidence(envelope.get("evidence"), errors)
    _validate_next_step(envelope.get("next_step"), errors)
    _validate_parts(envelope.get("parts"), errors)
    _validate_attempts(envelope.get("attempts"), errors)
    _validate_children(envelope.get("children"), errors)
    _require_str_list(envelope.get("warnings"), "warnings", errors)
    if not isinstance(envelope.get("extensions"), Mapping):
        errors.append("extensions must be an object")

    failure = envelope.get("failure")
    if failure is not None:
        _validate_failure(failure, errors)
    if outcome in FAILING_OUTCOMES and failure is None:
        errors.append(
            f"outcome {outcome!r} requires a failure block with a layer "
            "attribution"
        )
    if outcome == "success" and failure is not None:
        errors.append("outcome 'success' must not carry a failure block")
    next_step = envelope.get("next_step")
    if (
        outcome in FAILING_OUTCOMES
        and isinstance(next_step, Mapping)
        and not next_step.get("actions")
    ):
        errors.append(
            f"outcome {outcome!r} requires at least one next_step action, even "
            "if that action is only how to narrow an unknown"
        )
    parts = envelope.get("parts")
    if isinstance(parts, list) and parts:
        derived = outcome_from_parts(parts)
        if derived != outcome:
            errors.append(
                f"outcome {outcome!r} disagrees with parts (derived {derived!r})"
            )
    elif outcome == "partial":
        errors.append("outcome 'partial' requires parts describing each unit")

    unknown = sorted(set(envelope) - TOP_LEVEL_SET)
    if unknown:
        errors.append(
            "unknown top-level fields (use 'extensions' for additive data): "
            + ", ".join(unknown)
        )
    missing = sorted(TOP_LEVEL_SET - set(envelope))
    if missing:
        errors.append("missing top-level fields: " + ", ".join(missing))

    if errors:
        raise EnvelopeError("; ".join(errors))


def read_envelope(
    payload: Mapping[str, Any],
    *,
    accepted_versions: Iterable[str] = ACCEPTED_SCHEMA_VERSIONS,
) -> dict[str, Any]:
    """Lag-tolerant read path for consumers.

    Consumers lag producers, so a reader must not crash on a newer producer.
    An unrecognized ``schema_version`` or an unrecognized enum value is
    downgraded to ``unknown`` and recorded in ``compat_warnings`` instead of
    raising, while a same-version envelope is still validated strictly.
    """
    if not isinstance(payload, Mapping):
        raise EnvelopeError("envelope root must be an object")
    view = deepcopy(dict(payload))
    warnings: list[str] = []
    version = view.get("schema_version")
    if version in set(accepted_versions):
        validate_envelope(view)
        view["compat_warnings"] = []
        return view
    warnings.append(f"unrecognized schema_version {version!r}; read leniently")
    if view.get("outcome") not in OUTCOMES:
        warnings.append(f"unrecognized outcome {view.get('outcome')!r}")
        view["outcome"] = "failure"
    failure = view.get("failure")
    if isinstance(failure, Mapping) and failure.get("layer") not in LAYER_SET:
        warnings.append(f"unrecognized failure layer {failure.get('layer')!r}")
        failure = dict(failure)
        failure["layer"] = "unknown"
        view["failure"] = failure
    view["compat_warnings"] = warnings
    return view


# ---------------------------------------------------------------------------
# Serialization and emission
# ---------------------------------------------------------------------------


def dumps(envelope: Mapping[str, Any]) -> str:
    """Full-fidelity JSON. Safe only for untracked ``.vaws-local/`` writes."""
    return json.dumps(dict(envelope), ensure_ascii=False, indent=2, sort_keys=True)


def dumps_publishable(envelope: Mapping[str, Any]) -> str:
    """Redacted JSON, refusing to emit if a leak survives redaction."""
    redacted = redact(envelope)
    findings = leak_findings(redacted)
    if findings:
        raise EnvelopeRedactionError(
            "redacted envelope still carries identity: " + "; ".join(findings)
        )
    return json.dumps(redacted, ensure_ascii=False, indent=2, sort_keys=True)


def assert_publishable(envelope: Mapping[str, Any]) -> None:
    """Raise unless the envelope is already free of runtime identity."""
    findings = leak_findings(dict(envelope))
    if findings:
        raise EnvelopeRedactionError(
            "envelope carries runtime identity: " + "; ".join(findings)
        )


def emit(
    envelope: Mapping[str, Any],
    *,
    stream: Any = None,
    validate: bool = True,
) -> int:
    """Write exactly one envelope on ``stdout`` and return its exit code."""
    if validate:
        validate_envelope(envelope)
    target = stream if stream is not None else sys.stdout
    target.write(dumps(envelope) + "\n")
    target.flush()
    code = envelope.get("exit_code")
    return code if isinstance(code, int) else default_exit_code(
        str(envelope.get("outcome"))
    )


def progress(
    phase: str,
    message: str,
    *,
    sentinel: str = PROGRESS_SENTINEL,
    stream: Any = None,
    **extra: Any,
) -> None:
    """Write one bounded progress line on ``stderr``.

    Progress never goes to ``stdout``: a consumer must be able to parse
    ``stdout`` as exactly one JSON object without filtering.
    """
    payload: dict[str, Any] = {"phase": phase, "message": message}
    payload.update({key: value for key, value in extra.items() if value is not None})
    target = stream if stream is not None else sys.stderr
    target.write(sentinel + json.dumps(payload, ensure_ascii=False) + "\n")
    target.flush()
