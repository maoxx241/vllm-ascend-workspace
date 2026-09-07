"""Scrub endpoint identities from anything that may leave untracked state.

Host addresses, DNS names, ``user@host`` destinations and absolute local user
paths must never reach tracked files, knowledge candidates, or the default
stdout report. The redactor replaces them with stable labels so that reports
stay correlatable without being locatable.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")
# Full eight-group form, or a compressed form containing ``::``. Deliberately
# excludes ``hh:mm:ss`` style tokens so timings survive redaction.
IPV6_RE = re.compile(
    r"(?<![:\w])(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?)(?![:\w])",
    re.IGNORECASE,
)
USER_AT_HOST_RE = re.compile(r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+(?::\d{1,5})?\b")
LOCAL_USER_PATH_RE = re.compile(r"(?:/Users|/home)/[^/\s'\"]+")
FQDN_RE = re.compile(r"\b(?:[a-z0-9-]+\.){2,}[a-z]{2,}\b", re.IGNORECASE)

REDACTED_ADDRESS = "<host>"
REDACTED_PATH = "<local-home>"


class Redactor:
    """Replace known endpoint identities with labels, then generic scrubbing."""

    def __init__(self, known: Mapping[str, str] | None = None) -> None:
        # Longest identities first so ``host:port`` wins over bare ``host``.
        self._known = sorted((known or {}).items(), key=lambda item: -len(item[0]))

    @classmethod
    def for_endpoints(cls, endpoints: Iterable[Mapping[str, Any]]) -> "Redactor":
        known: dict[str, str] = {}
        for item in endpoints:
            label = str(item.get("label") or REDACTED_ADDRESS)
            host = str(item.get("host") or "")
            port = item.get("port")
            if host and port is not None:
                known[f"{host}:{port}"] = label
            if host:
                known.setdefault(host, label)
            hostname = item.get("hostname")
            if hostname:
                known.setdefault(str(hostname), label)
        return cls(known)

    def text(self, value: str) -> str:
        for identity, label in self._known:
            if identity and identity in value:
                value = value.replace(identity, label)
        value = USER_AT_HOST_RE.sub(lambda m: m.group(0).split("@", 1)[0] + "@" + REDACTED_ADDRESS, value)
        value = IPV4_RE.sub(REDACTED_ADDRESS, value)
        value = IPV6_RE.sub(REDACTED_ADDRESS, value)
        value = LOCAL_USER_PATH_RE.sub(REDACTED_PATH, value)
        value = FQDN_RE.sub(REDACTED_ADDRESS, value)
        return value

    def value(self, value: Any) -> Any:  # noqa: ANN401
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


def contains_address(value: Any) -> bool:  # noqa: ANN401
    """True when an IPv4/IPv6 literal or user@host survives in ``value``."""
    if isinstance(value, str):
        return bool(IPV4_RE.search(value) or IPV6_RE.search(value) or USER_AT_HOST_RE.search(value))
    if isinstance(value, Mapping):
        return any(contains_address(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_address(item) for item in value)
    return False
