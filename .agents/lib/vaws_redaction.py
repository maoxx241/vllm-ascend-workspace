#!/usr/bin/env python3
"""Source-side redaction ruleset for federated knowledge (profile r1).

The federated knowledge commons is public and its history cannot be recalled,
so redaction runs in the contributing fork *before* anything is proposed
upstream. This module is the fork-side implementation of that gate.

Two severities, because the three-layer trust model has two different
questions:

``block``
    Must never be written anywhere, including tracked project-layer files.
    IP addresses, MAC addresses, e-mail addresses, user home paths, secrets.

``export``
    Legitimate inside a fork's ``project`` layer, but must never leave it.
    Internal mount paths, container names, ticket identifiers. These are the
    "not publishable" facts the three-layer model explicitly keeps local.

``profile`` is versioned (``r1``) and recorded on every exported entry as
``provenance.redaction_profile`` so the main repo can re-scan the corpus in
bulk when the ruleset tightens.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

REDACTION_PROFILE = "r1"

# Loopback, unspecified and RFC5737/RFC3849 documentation addresses identify
# nothing. Keeping them readable matters: the most useful resolution in the
# current corpus is literally "map the hostname to 127.0.0.1".
_ALLOWED_IP_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("2001:db8::/32"),
)

# Public code / package / documentation hosts an entry may legitimately cite.
_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "gitee.com",
        "gitlab.com",
        "hf.co",
        "huggingface.co",
        "modelscope.cn",
        "pytorch.org",
        "python.org",
        "json-schema.org",
        "docs.python.org",
        "readthedocs.io",
        "example.com",
        "example.invalid",
    }
)

# Absolute POSIX prefixes that describe a container/OS layout rather than a
# person or an internal deployment.
_PUBLIC_PATH_PREFIXES = (
    "/etc/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/tmp/",
    "/usr/",
    "/bin/",
    "/sbin/",
    "/lib/",
    "/opt/",
    "/var/log/",
    "/workspace/",
    "/vllm-workspace/",
)

_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b", re.IGNORECASE)
_MAC_RE = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_USER_AT_HOST_RE = re.compile(r"\b[a-z_][a-z0-9_-]{0,31}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\b", re.IGNORECASE)
_USER_HOME_RE = re.compile(
    r"(?:/home/[^/\s]+|/Users/[^/\s]+|/root(?![\w-])|[A-Za-z]:[\\/]Users[\\/][^\\/\s]+)"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w./])/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.+-]+)+")
_FQDN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\."
    r"(?:com|cn|net|org|io|dev|local|internal|intra|corp|lan|test)\b",
    re.IGNORECASE,
)
# Container *instance* names, not project names. ``vaws-knowledge`` and
# ``vllm-ascend-serving`` are public identifiers; ``vaws-sess-3f9a`` is not.
_CONTAINER_NAME_RE = re.compile(
    r"\b(?:vaws|npu|session)-(?:sess|session|task|run|job|node|dev|c)[a-z0-9_-]*\b",
    re.IGNORECASE,
)
_CONTAINER_FLAG_RE = re.compile(r"--name[=\s]+([A-Za-z0-9][\w.-]{2,})")
_TICKET_RE = re.compile(
    r"\b(?:jira|issue|ticket|case|dts|redmine)[-_ #]?\d{3,}\b", re.IGNORECASE
)
_EMPLOYEE_ID_RE = re.compile(r"\b[a-z]{1,2}\d{8}\b", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|auth|credential|pass(?:word)?|secret|token)(?:_|$)",
    re.IGNORECASE,
)
_SECRET_VALUE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"://[^/\s:@]+:[^/\s:@]+@"),
)

BLOCK = "block"
EXPORT = "export"

RULES: tuple[tuple[str, str, str], ...] = (
    ("secret-value", BLOCK, "credential-shaped value"),
    ("secret-key-name", BLOCK, "credential-shaped field name"),
    ("ip-address", BLOCK, "routable or private IP address"),
    ("mac-address", BLOCK, "MAC address"),
    ("email-address", BLOCK, "e-mail address"),
    ("user-at-host", BLOCK, "user@host login target"),
    ("user-home-path", BLOCK, "absolute path revealing a user account"),
    ("hostname", BLOCK, "hostname or FQDN"),
    ("internal-mount-path", EXPORT, "absolute path outside the public OS layout"),
    ("container-name", EXPORT, "container or session instance name"),
    ("ticket-id", EXPORT, "ticket or case identifier"),
    ("possible-employee-id", EXPORT, "identifier shaped like an employee id"),
)
_SEVERITY = {name: severity for name, severity, _ in RULES}


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


def _ip_is_allowed(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return True  # not an address at all (e.g. a version string)
    return any(address in network for network in _ALLOWED_IP_NETWORKS)


def _scan_text(text: str, path: str) -> list[Finding]:
    findings: list[Finding] = []

    def add(rule: str, sample: str) -> None:
        findings.append(Finding(rule, _SEVERITY[rule], path, mask(sample)))

    for pattern in _SECRET_VALUE_RES:
        match = pattern.search(text)
        if match:
            add("secret-value", match.group(0))
            break

    for candidate in _IPV4_RE.findall(text):
        if not _ip_is_allowed(candidate):
            add("ip-address", candidate)
    for candidate in _MAC_RE.findall(text):
        add("mac-address", candidate)
    for candidate in _IPV6_RE.findall(text):
        if not _ip_is_allowed(candidate):
            add("ip-address", candidate)

    emails = set(_EMAIL_RE.findall(text))
    for candidate in emails:
        domain = candidate.rsplit("@", 1)[-1].lower()
        if domain not in _ALLOWED_HOSTS:
            add("email-address", candidate)
    for candidate in _USER_AT_HOST_RE.findall(text):
        if candidate in emails:
            continue
        domain = candidate.rsplit("@", 1)[-1].lower()
        if domain not in _ALLOWED_HOSTS:
            add("user-at-host", candidate)

    for candidate in _USER_HOME_RE.findall(text):
        add("user-home-path", candidate)

    for candidate in _FQDN_RE.findall(text):
        if candidate.lower() in _ALLOWED_HOSTS:
            continue
        # Version-ish and file-ish tokens ("2.5.1.dev", "config.json") are not
        # hostnames; require a known network suffix and no digit-only labels.
        labels = candidate.lower().split(".")
        if any(label.isdigit() for label in labels):
            continue
        add("hostname", candidate)

    for candidate in _ABSOLUTE_PATH_RE.findall(text):
        lowered = candidate.lower()
        if _USER_HOME_RE.match(candidate):
            continue
        if any(lowered.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES):
            continue
        add("internal-mount-path", candidate)

    for candidate in _CONTAINER_NAME_RE.findall(text):
        add("container-name", candidate)
    for candidate in _CONTAINER_FLAG_RE.findall(text):
        add("container-name", candidate)
    for candidate in _TICKET_RE.findall(text):
        add("ticket-id", candidate)
    for candidate in _EMPLOYEE_ID_RE.findall(text):
        add("possible-employee-id", candidate)

    return findings


def scan(value: Any, *, path: str = "payload") -> list[Finding]:
    """Recursively scan a JSON-compatible payload for redaction findings."""

    findings: list[Finding] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if _SECRET_KEY_RE.search(key_text):
                findings.append(
                    Finding(
                        "secret-key-name",
                        _SEVERITY["secret-key-name"],
                        child_path,
                        mask(key_text),
                    )
                )
            # Keys carry values too: a path used as a map key leaks the user
            # account just as effectively as the value would.
            findings.extend(_scan_text(key_text, child_path))
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
            + REDACTION_PROFILE
            + " blocks this payload: "
            + "; ".join(f"{item.path}: {item.rule} ({item.masked})" for item in findings)
        )


def require_exportable(value: Any, *, path: str = "payload") -> None:
    """Raise when a payload must not leave this fork."""

    findings = export_findings(scan(value, path=path))
    if findings:
        raise RedactionError(
            "redaction profile "
            + REDACTION_PROFILE
            + " blocks this export: "
            + "; ".join(f"{item.path}: {item.rule} ({item.masked})" for item in findings)
        )


def ruleset() -> list[dict[str, str]]:
    """Machine-readable description of the active profile."""

    return [
        {"rule": name, "severity": severity, "description": description}
        for name, severity, description in RULES
    ]
