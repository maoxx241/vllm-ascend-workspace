#!/usr/bin/env python3
"""Scaffold redaction policy over ``vaws_knowledge.redact``.

Detection rules live in the commons package. This module does not keep
regexes or allowlists; it only applies local policy on top of
``vaws_knowledge.redact.scan_text``:

``block``
    Must never be written anywhere, including tracked project-layer files.

``export``
    Legitimate inside a fork's ``project`` layer, but must never leave it.

``REDACTION_PROFILE`` is whatever the installed package declares. Exported
entries record it as ``provenance.redaction_profile`` so a later ruleset
bump can trigger a bulk re-scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

BLOCK = "block"
EXPORT = "export"


def _commons() -> Any:
    # Imported on first use so tracked-leak-guard CI (no venv) can still
    # import this module through the knowledge service for SECRET_* constants.
    import vaws_knowledge.redact as redact

    return redact


def __getattr__(name: str) -> Any:
    if name == "REDACTION_PROFILE":
        return _commons().REDACTION_PROFILE
    if name == "RULES":
        commons = _commons()
        return tuple(
            (rule.id, severity_for(rule.id), rule.description) for rule in commons.RULES
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_BLOCK_EXACT = frozenset(
    {
        "mac-address",
        "ipv4-address",
        "ipv6-address",
        "email-address",
        "username-at-host",
        "user-tilde-path",
    }
)
_BLOCK_PREFIXES = (
    "credential-",
    "user-path",
    "username-",
    "hostname-",
)


def severity_for(rule_id: str) -> str:
    """Map a commons rule id to scaffold BLOCK/EXPORT. Unknown ids are EXPORT."""

    if rule_id in _BLOCK_EXACT or any(rule_id.startswith(prefix) for prefix in _BLOCK_PREFIXES):
        return BLOCK
    return EXPORT


class RedactionError(ValueError):
    """Raised when a payload cannot be redacted safely."""


@dataclass(frozen=True)
class Finding:
    """One redaction hit. ``masked`` never reproduces the full value."""

    rule: str
    severity: str
    path: str
    masked: str

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "path": self.path,
            "masked": self.masked,
        }


def mask(value: str) -> str:
    """Return a non-reversible excerpt.

    A redaction report that prints the offending value defeats the purpose, so
    only a short prefix survives.
    """

    text = " ".join(str(value).split())
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * min(len(text) - 4, 8)}{text[-2:]}"


def _scan_text(text: str, path: str) -> list[Finding]:
    return [
        Finding(hit.rule, severity_for(hit.rule), path, mask(hit.value))
        for hit in _commons().scan_text(text, allow=None, path=path)
    ]


def scan(value: Any, *, path: str = "payload") -> list[Finding]:
    """Recursively scan a JSON-compatible payload for redaction findings."""

    findings: list[Finding] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            findings.extend(_scan_text(str(key), child_path))
            findings.extend(scan(child, path=child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            findings.extend(scan(child, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        findings.extend(_scan_text(value, path))
    return findings


def blocking_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Findings that must not be written even to tracked project files."""

    return [finding for finding in findings if finding.severity == BLOCK]


def export_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Every finding that must stop an upstream proposal."""

    return list(findings)


def require_writable(value: Any, *, path: str = "payload") -> None:
    """Raise when a payload contains a ``block``-severity finding."""

    findings = blocking_findings(scan(value, path=path))
    if findings:
        raise RedactionError(
            "redaction profile "
            + _commons().REDACTION_PROFILE
            + " blocks this payload: "
            + "; ".join(f"{item.path}: {item.rule} ({item.masked})" for item in findings)
        )


def require_exportable(value: Any, *, path: str = "payload") -> None:
    """Raise when a payload must not leave this fork."""

    findings = export_findings(scan(value, path=path))
    if findings:
        raise RedactionError(
            "redaction profile "
            + _commons().REDACTION_PROFILE
            + " blocks this export: "
            + "; ".join(f"{item.path}: {item.rule} ({item.masked})" for item in findings)
        )


def ruleset() -> list[dict[str, str]]:
    """Machine-readable description of the active profile."""

    return [
        {
            "rule": rule.id,
            "severity": severity_for(rule.id),
            "description": rule.description,
        }
        for rule in _commons().RULES
    ]
