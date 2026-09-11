#!/usr/bin/env python3
"""Shared detection library for the tracked-file leak guard.

The scaffold drives remote NPU hosts over SSH, so its working state is full of
host addresses, container names, and absolute paths. `AGENTS.md` already forbids
writing secrets into tracked files, but nothing enforced it and the corpus
leaked anyway.

This module extends the secret-shaped patterns already used for knowledge
documents (`vaws_knowledge.SECRET_KEY_RE` / `SECRET_VALUE_RES`) with the
categories those patterns never covered: network addresses, MAC addresses,
person- or org-revealing absolute paths, e-mail addresses, internal host and
container names, and internal identity tokens.

Policy is data, not code: every allowance lives in a reviewable YAML file with a
mandatory justification. See `.agents/leak-guard/allowlist.yaml`.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

# Reuse, rather than restate, the secret-shaped patterns that already gate
# knowledge candidates. This module only adds the categories they miss.
# Imported lazily so --install/--status can run before `python .agents/scripts/vaws_deps.py sync`; every scan
# path calls require_knowledge_redact() and refuses if the package is absent.
KNOWLEDGE_PACKAGE = "vaws_knowledge"
KNOWLEDGE_REMEDY = "python .agents/scripts/vaws_deps.py sync"
KNOWLEDGE_MISSING = (
    "vaws_knowledge is not importable; the leak scanner requires the "
    "installed vaws-knowledge package. Install it with `python .agents/scripts/vaws_deps.py sync` "
    "(or `uv run python3 .agents/scripts/tracked_leak_scan.py`, which syncs first)."
)
_knowledge_redact = None

SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|auth|credential|pass(?:word)?|secret|token)(?:_|$)",
    re.IGNORECASE,
)

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
MIN_JUSTIFICATION_CHARS = 20


def default_policy_file(repo_root: Path | str) -> Path:
    """Return the committed-tree policy path for `repo_root`, even if absent."""

    return Path(repo_root) / ".agents" / "leak-guard" / "allowlist.yaml"


DEFAULT_POLICY_PATH = default_policy_file(ROOT)

# Ordered by review priority. When two rules overlap on the same span, the
# earlier category wins so one leak yields one actionable finding.
CATEGORIES: tuple[str, ...] = (
    "secret-value",
    "secret-key",
    "ipv4",
    "ipv6",
    "mac-address",
    "absolute-user-path",
    "email",
    "internal-identifier",
    "internal-hostname",
    "container-name",
)
CATEGORY_ORDER = {name: index for index, name in enumerate(CATEGORIES)}


class LeakGuardError(RuntimeError):
    """Raised when the policy file or a git invocation cannot be trusted."""


def require_knowledge_redact():
    """Return ``vaws_knowledge.redact``, or refuse. Never scan with fewer rules."""

    global _knowledge_redact
    if _knowledge_redact is not None:
        return _knowledge_redact
    try:
        import vaws_knowledge.redact as knowledge_redact
    except ModuleNotFoundError as exc:
        raise LeakGuardError(KNOWLEDGE_MISSING) from exc
    _knowledge_redact = knowledge_redact
    return knowledge_redact


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

IPV4_RE = re.compile(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")
# Deliberately loose: collect hex/colon runs and let `ipaddress` decide. A
# hand-written IPv6 grammar is where silent misses hide.
IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:.])[0-9A-Fa-f:]{2,45}(?::(?:\d{1,3}\.){3}\d{1,3})?"
    r"(?:%[A-Za-z0-9_.-]+)?(?![0-9A-Za-z:.])"
)
MAC_RE = re.compile(
    r"(?<![0-9A-Za-z:_-])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Za-z:_-])"
    r"|(?<![0-9A-Za-z:_-])(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}(?![0-9A-Za-z:_-])"
)
DEFAULT_SCANNED_PATH_ROOTS: tuple[str, ...] = ("Users", "home", "root")


def build_user_path_re(roots: Sequence[str]) -> re.Pattern[str]:
    """Build the home-path rule for the configured absolute roots.

    Which roots are scanned is policy, not code: `/vllm-workspace` and other
    shared container mounts are deliberately *not* scanned (see
    `.agents/leak-guard/allowlist.yaml`), and a maintainer can add them.
    """

    alternatives = "|".join(re.escape(root.strip("/")) for root in roots)
    return re.compile(
        r"(?<![A-Za-z0-9._~-])(/(?:" + alternatives + r"))/([A-Za-z0-9][A-Za-z0-9._@+-]*)"
    )


USER_PATH_RE = build_user_path_re(DEFAULT_SCANNED_PATH_ROOTS)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}"
    r"(?![A-Za-z0-9.-])"
)

# A secret-shaped key with a literal-looking value. The generic
# `token = something` shape that dominates Python source is rejected below by
# `_is_placeholder_value`; unquoted bare identifiers never count as a value.
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.\[\]'\"-]{0,48}?"
    r"(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|credential)"
    r"[A-Za-z0-9_.\[\]'\"-]{0,48}?)"
    r"\s*(?:[:=]|=>)\s*"
    r"(?P<value>\"[^\"\n]{6,120}\"|'[^'\n]{6,120}'|[A-Za-z0-9+/=_.~-]{8,120})",
    re.IGNORECASE,
)
# High-precision credential values that complement `SECRET_VALUE_RES`.
EXTRA_SECRET_VALUE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # Written without the trailing dashes so a truncated paste is still caught,
    # and so this module does not match its own source.
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY"),
    re.compile(r"\bPuTTY-User-Key-File-\d\b"),
)

PLACEHOLDER_VALUE_RE = re.compile(
    r"^(?:"
    r"[*x.\-_]+|"
    r"(?:redacted|placeholder|example|changeme|change_me|your[_-]?\w+|none|null|"
    r"true|false|undefined|password|passwd|secret|token|api[_-]?key|dummy|fake|"
    r"test|sample|value|string|integer|boolean|object|array|required|optional)"
    r")$",
    re.IGNORECASE,
)
IDENTIFIER_VALUE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
ENV_NAME_VALUE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
SHORT_KEBAB_VALUE_RE = re.compile(r"^[a-z]{2,}-[a-z]{2,}$")
WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,12}$")
# A DNS name is either quoted/embedded in a URL or SSH target, or it carries a
# host-shaped first label (digit, hyphen, or a third label). Python attribute
# access such as `args.local` satisfies neither.
HOSTNAME_LEFT_CONTEXT = "\"'@=:/"
HOSTNAME_RIGHT_CONTEXT = "\"'/"
HOST_SHAPED_LABEL_RE = re.compile(r"^[a-z0-9]*[0-9-][a-z0-9-]*\.")

ALLOWED_IPV4_NETWORKS: tuple[str, ...] = (
    "127.0.0.0/8",  # loopback
    "0.0.0.0/32",  # unspecified / bind-all
    "255.255.255.255/32",  # broadcast
    "192.0.2.0/24",  # RFC 5737 TEST-NET-1
    "198.51.100.0/24",  # RFC 5737 TEST-NET-2
    "203.0.113.0/24",  # RFC 5737 TEST-NET-3
)
ALLOWED_IPV6_NETWORKS: tuple[str, ...] = (
    "::1/128",  # loopback
    "::/128",  # unspecified
    "2001:db8::/32",  # RFC 3849 documentation
)
ALLOWED_MAC_PREFIXES: tuple[str, ...] = (
    "00:00:5e:00:53",  # RFC 7042 documentation range
    "00:00:00:00:00:00",  # null address
    "ff:ff:ff:ff:ff:ff",  # broadcast
)
ALLOWED_EMAIL_DOMAINS: tuple[str, ...] = (
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "example.invalid",
    "invalid",
    "localhost",
)
ALLOWED_PATH_PREFIXES: tuple[str, ...] = ()
ALLOWED_PATH_SEGMENT_PATTERNS: tuple[str, ...] = ()
DEFAULT_EXCLUDED_PATH_GLOBS: tuple[str, ...] = (
    "vllm/**",
    "vllm-ascend/**",
    ".vaws-local/**",
    ".vaws-runtime/**",
    ".remote-dev/state/**",
)

# Internal-name rules. Each is data so a maintainer can extend coverage through
# the policy file instead of patching this module.
DEFAULT_NAME_RULES: tuple[dict[str, str], ...] = (
    {
        "id": "internal-domain-suffix",
        "category": "internal-hostname",
        # A private DNS suffix. The negative lookahead keeps `settings.local`
        # style file names out only when a file extension follows.
        "pattern": (
            r"(?<![A-Za-z0-9._-])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
            r"\.(?:local|lan|internal|intra|intranet|corp|home|localdomain)"
            r"(?![A-Za-z0-9_-])(?!\.[A-Za-z]{2,5}(?![A-Za-z0-9]))"
        ),
    },
    {
        "id": "internal-identity-token",
        "category": "internal-identifier",
        # Employee/staff id shape: a short letter prefix plus a long numeric
        # tail. This is the rule that found an identity token inside a
        # shared-storage path, where no home-directory rule would have looked.
        "pattern": r"(?<![A-Za-z0-9_])[a-z]{1,3}\d{6,10}(?![A-Za-z0-9_])",
    },
    {
        "id": "identity-bearing-container-name",
        "category": "container-name",
        # Container names are generated from session ids and carry no private
        # information by themselves. Only names that embed an identity token or
        # an address-derived suffix are treated as a leak.
        "pattern": (
            r"(?<![A-Za-z0-9._-])(?:vaws|vllm[-_]?ascend|ascend|npu)"
            r"[-_][A-Za-z0-9._-]*"
            r"(?:[a-z]{1,3}\d{6,10}|(?:\d{1,3}[-_]){2}\d{1,3})"
            r"[A-Za-z0-9._-]*"
        ),
    },
)


# --------------------------------------------------------------------------
# Minimal YAML reader
# --------------------------------------------------------------------------


def load_yaml_mapping(text: str) -> dict[str, Any]:
    """Parse the policy file, preferring PyYAML and falling back to a subset.

    The pre-commit hook must work on a checkout with nothing installed, so this
    module never hard-depends on PyYAML. The fallback understands exactly the
    shapes the policy file uses: nested mappings, lists of mappings, scalars,
    and `#` comments.
    """

    try:  # pragma: no cover - depends on the local interpreter
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return _parse_yaml_subset(text)
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise LeakGuardError("policy file root must be a mapping")
    return data


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        return inner.replace('\\"', '"') if value[0] == '"' else inner
    if value in {"", "~", "null"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        return [_scalar(item) for item in _split_flow(body)] if body else []
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _split_flow(body: str) -> list[str]:
    items: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        items.append("".join(current))
    return [item for item in (part.strip() for part in items) if item]


def _strip_comment(line: str) -> str:
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        body = _strip_comment(raw).rstrip()
        if not body.strip():
            continue
        lines.append((len(body) - len(body.lstrip(" ")), body.strip()))
    value, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise LeakGuardError(f"policy file has unsupported YAML at line {index + 1}")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LeakGuardError("policy file root must be a mapping")
    return value


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return None, index
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, lines[index][0])
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, body = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise LeakGuardError(f"policy file has inconsistent indentation: {body!r}")
        if ":" not in body:
            raise LeakGuardError(f"policy file line is not a mapping entry: {body!r}")
        key, _, rest = body.partition(":")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest:
            result[key] = _scalar(rest)
            continue
        if index < len(lines) and lines[index][0] > line_indent:
            child, index = _parse_block(lines, index, lines[index][0])
            result[key] = child
        elif index < len(lines) and lines[index][0] == line_indent and lines[index][1].startswith("- "):
            child, index = _parse_sequence(lines, index, line_indent)
            result[key] = child
        else:
            result[key] = None
    return result, index


def _parse_sequence(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, body = lines[index]
        if line_indent != indent or not body.startswith("- "):
            break
        item = body[2:].strip()
        index += 1
        if ":" in item and not (item.startswith('"') or item.startswith("'")):
            key, _, rest = item.partition(":")
            entry: dict[str, Any] = {}
            if rest.strip():
                entry[key.strip()] = _scalar(rest)
            else:
                if index < len(lines) and lines[index][0] > indent:
                    child, index = _parse_block(lines, index, lines[index][0])
                    entry[key.strip()] = child
                else:
                    entry[key.strip()] = None
            child_indent = indent + 2
            while index < len(lines) and lines[index][0] >= child_indent and not lines[index][1].startswith("- "):
                nested, index = _parse_mapping(lines, index, lines[index][0])
                if isinstance(nested, dict):
                    entry.update(nested)
            result.append(entry)
            continue
        result.append(_scalar(item))
    return result, index


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowEntry:
    id: str
    path_glob: str
    categories: tuple[str, ...]
    justification: str
    literal: str | None = None
    pattern: re.Pattern[str] | None = None
    remediation: str | None = None

    def matches(self, path: str, category: str, text: str) -> bool:
        if not glob_matches(self.path_glob, path):
            return False
        if "*" not in self.categories and category not in self.categories:
            return False
        if self.literal is not None and self.literal != text:
            return False
        if self.pattern is not None and not self.pattern.search(text):
            return False
        return True


@dataclass(frozen=True)
class ScopedExclusion:
    id: str
    path_glob: str
    categories: tuple[str, ...]
    justification: str

    def covers(self, path: str) -> bool:
        return glob_matches(self.path_glob, path)

    def excludes(self, category: str) -> bool:
        return "*" in self.categories or category in self.categories


SELF_REFERENCE_ENTRY = AllowEntry(
    id="policy-self-reference",
    path_glob=".agents/leak-guard/allowlist.yaml",
    categories=("*",),
    justification=(
        "The policy file quotes the exact values it allows; reporting them there "
        "would make the allowlist unwritable."
    ),
)


@dataclass(frozen=True)
class NameRule:
    id: str
    category: str
    pattern: re.Pattern[str]


@dataclass
class Policy:
    source: Path | None = None
    allowed_ipv4_networks: tuple[Any, ...] = ()
    allowed_ipv6_networks: tuple[Any, ...] = ()
    allowed_mac_prefixes: tuple[str, ...] = ALLOWED_MAC_PREFIXES
    allowed_email_domains: tuple[str, ...] = ALLOWED_EMAIL_DOMAINS
    allowed_path_prefixes: tuple[str, ...] = ()
    allowed_path_segment_patterns: tuple[re.Pattern[str], ...] = ()
    scanned_path_roots: tuple[str, ...] = DEFAULT_SCANNED_PATH_ROOTS
    user_path_re: re.Pattern[str] = USER_PATH_RE
    excluded_path_globs: tuple[str, ...] = DEFAULT_EXCLUDED_PATH_GLOBS
    name_rules: tuple[NameRule, ...] = ()
    entries: tuple[AllowEntry, ...] = ()
    scoped_exclusions: tuple[ScopedExclusion, ...] = ()
    max_file_bytes: int = MAX_FILE_BYTES
    self_path_suffix: str | None = None
    used_entry_ids: set[str] = field(default_factory=set)

    def is_self(self, path: str) -> bool:
        if not self.self_path_suffix:
            return False
        return path == self.self_path_suffix or path.endswith("/" + self.self_path_suffix)

    def declares(self, text: str) -> bool:
        """True when the policy file itself quotes this value in an entry.

        An allowlist has to name the values it allows. Without this, the policy
        file would report itself and become impossible to write.
        """

        for entry in self.entries:
            if entry.literal is not None and entry.literal == text:
                return True
            if entry.pattern is not None and entry.pattern.search(text):
                return True
        return False

    def is_path_excluded(self, path: str) -> bool:
        return any(glob_matches(pattern, path) for pattern in self.excluded_path_globs)

    def scoped_categories(self, path: str) -> tuple[ScopedExclusion, ...]:
        return tuple(item for item in self.scoped_exclusions if item.covers(path))

    def allow_entry_for(self, path: str, category: str, text: str) -> AllowEntry | None:
        if self.is_self(path) and self.declares(text):
            return SELF_REFERENCE_ENTRY
        for entry in self.entries:
            if entry.matches(path, category, text):
                self.used_entry_ids.add(entry.id)
                return entry
        return None


def glob_matches(pattern: str, path: str) -> bool:
    return re.fullmatch(_glob_to_regex(pattern), path) is not None


_GLOB_CACHE: dict[str, str] = {}


def _glob_to_regex(pattern: str) -> str:
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out.append(r"(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            out.append(r".*")
            index += 2
            continue
        if char == "*":
            out.append(r"[^/]*")
        elif char == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    regex = "".join(out)
    _GLOB_CACHE[pattern] = regex
    return regex


def _require_str(value: Any, *, where: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LeakGuardError(f"{where}: {name} must be a non-empty string")
    return value.strip()


def _require_categories(value: Any, *, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list) and value:
        raw = value
    else:
        raise LeakGuardError(f"{where}: categories must be a non-empty list")
    categories: list[str] = []
    for item in raw:
        name = _require_str(item, where=where, name="category")
        if name != "*" and name not in CATEGORY_ORDER:
            raise LeakGuardError(f"{where}: unknown category {name!r}")
        categories.append(name)
    return tuple(categories)


def _require_justification(value: Any, *, where: str) -> str:
    text = _require_str(value, where=where, name="justification")
    if len(text) < MIN_JUSTIFICATION_CHARS:
        raise LeakGuardError(
            f"{where}: justification must be at least {MIN_JUSTIFICATION_CHARS} characters"
        )
    return text


def _unknown_keys(mapping: Mapping[str, Any], allowed: Iterable[str], *, where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise LeakGuardError(f"{where}: unsupported keys: {', '.join(unknown)}")


def _networks(values: Sequence[Any], *, where: str, family: int) -> tuple[Any, ...]:
    parsed = []
    for item in values:
        text = _require_str(item, where=where, name="network")
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise LeakGuardError(f"{where}: invalid network {text!r}: {exc}") from exc
        if network.version != family:
            raise LeakGuardError(f"{where}: {text!r} is not an IPv{family} network")
        parsed.append(network)
    return tuple(parsed)


def default_policy() -> Policy:
    return Policy(
        allowed_ipv4_networks=_networks(
            list(ALLOWED_IPV4_NETWORKS), where="defaults", family=4
        ),
        allowed_ipv6_networks=_networks(
            list(ALLOWED_IPV6_NETWORKS), where="defaults", family=6
        ),
        name_rules=tuple(
            NameRule(item["id"], item["category"], re.compile(item["pattern"]))
            for item in DEFAULT_NAME_RULES
        ),
    )


def load_policy(path: Path | None) -> Policy:
    """Load and validate the policy file, failing closed on anything unclear."""

    policy = default_policy()
    if path is None:
        return policy
    if not path.is_file():
        raise LeakGuardError(f"policy file not found: {path}")
    data = load_yaml_mapping(path.read_text(encoding="utf-8"))
    _unknown_keys(
        data,
        ("schema_version", "settings", "allowlist", "scoped_exclusions"),
        where=str(path),
    )
    version = data.get("schema_version")
    # bool is a subclass of int, so `True == 1` must not pass as schema_version.
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise LeakGuardError(
            f"{path}: schema_version must be {SCHEMA_VERSION}, got {version!r}"
        )

    policy.source = path
    policy.self_path_suffix = "/".join(path.resolve().parts[-3:])
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        raise LeakGuardError(f"{path}: settings must be a mapping")
    _unknown_keys(
        settings,
        (
            "allowed_ipv4_networks",
            "allowed_ipv6_networks",
            "allowed_mac_prefixes",
            "allowed_email_domains",
            "allowed_absolute_path_prefixes",
            "allowed_absolute_path_segment_patterns",
            "scanned_absolute_path_roots",
            "excluded_path_globs",
            "name_rules",
            "max_file_bytes",
        ),
        where=f"{path}: settings",
    )
    where = f"{path}: settings"
    if settings.get("allowed_ipv4_networks"):
        policy.allowed_ipv4_networks += _networks(
            settings["allowed_ipv4_networks"], where=where, family=4
        )
    if settings.get("allowed_ipv6_networks"):
        policy.allowed_ipv6_networks += _networks(
            settings["allowed_ipv6_networks"], where=where, family=6
        )
    if settings.get("allowed_mac_prefixes"):
        policy.allowed_mac_prefixes += tuple(
            _require_str(item, where=where, name="mac prefix").lower()
            for item in settings["allowed_mac_prefixes"]
        )
    if settings.get("allowed_email_domains"):
        policy.allowed_email_domains += tuple(
            _require_str(item, where=where, name="email domain").lower()
            for item in settings["allowed_email_domains"]
        )
    policy.allowed_path_prefixes = _load_justified_values(
        settings.get("allowed_absolute_path_prefixes"),
        key="prefix",
        where=f"{where}.allowed_absolute_path_prefixes",
    )
    policy.allowed_path_segment_patterns = tuple(
        re.compile(item)
        for item in _load_justified_values(
            settings.get("allowed_absolute_path_segment_patterns"),
            key="pattern",
            where=f"{where}.allowed_absolute_path_segment_patterns",
        )
    )
    if settings.get("scanned_absolute_path_roots"):
        policy.scanned_path_roots = tuple(
            _require_str(item, where=where, name="absolute path root").strip("/")
            for item in settings["scanned_absolute_path_roots"]
        )
        policy.user_path_re = build_user_path_re(policy.scanned_path_roots)
    if settings.get("excluded_path_globs"):
        policy.excluded_path_globs += tuple(
            _require_str(item, where=where, name="excluded glob")
            for item in settings["excluded_path_globs"]
        )
    if settings.get("name_rules"):
        policy.name_rules += tuple(
            _load_name_rule(item, where=f"{where}.name_rules")
            for item in settings["name_rules"]
        )
    if settings.get("max_file_bytes") is not None:
        raw_max = settings["max_file_bytes"]
        if isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max <= 0:
            raise LeakGuardError(f"{where}: max_file_bytes must be a positive integer")
        policy.max_file_bytes = raw_max

    policy.entries = tuple(_load_entries(data.get("allowlist"), where=str(path)))
    policy.scoped_exclusions = tuple(
        _load_exclusions(data.get("scoped_exclusions"), where=str(path))
    )
    return policy


def _load_justified_values(raw: Any, *, key: str, where: str) -> tuple[str, ...]:
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise LeakGuardError(f"{where}: must be a list of mappings")
    values: list[str] = []
    for index, item in enumerate(raw):
        item_where = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise LeakGuardError(f"{item_where}: must be a mapping with {key} and justification")
        _unknown_keys(item, (key, "justification"), where=item_where)
        values.append(_require_str(item.get(key), where=item_where, name=key))
        _require_justification(item.get("justification"), where=item_where)
    return tuple(values)


def _load_name_rule(item: Any, *, where: str) -> NameRule:
    if not isinstance(item, dict):
        raise LeakGuardError(f"{where}: each name rule must be a mapping")
    _unknown_keys(item, ("id", "category", "pattern", "justification"), where=where)
    rule_id = _require_str(item.get("id"), where=where, name="id")
    category = _require_str(item.get("category"), where=where, name="category")
    if category not in CATEGORY_ORDER:
        raise LeakGuardError(f"{where}: unknown category {category!r}")
    _require_justification(item.get("justification"), where=where)
    try:
        pattern = re.compile(_require_str(item.get("pattern"), where=where, name="pattern"))
    except re.error as exc:
        raise LeakGuardError(f"{where}: invalid regex: {exc}") from exc
    return NameRule(rule_id, category, pattern)


def _load_entries(raw: Any, *, where: str) -> Iterator[AllowEntry]:
    if not raw:
        return
    if not isinstance(raw, list):
        raise LeakGuardError(f"{where}: allowlist must be a list")
    seen: set[str] = set()
    for index, item in enumerate(raw):
        item_where = f"{where}: allowlist[{index}]"
        if not isinstance(item, dict):
            raise LeakGuardError(f"{item_where}: entry must be a mapping")
        _unknown_keys(
            item,
            ("id", "path_glob", "categories", "match", "match_regex", "justification", "remediation"),
            where=item_where,
        )
        entry_id = _require_str(item.get("id"), where=item_where, name="id")
        if entry_id in seen:
            raise LeakGuardError(f"{item_where}: duplicate allowlist id {entry_id!r}")
        seen.add(entry_id)
        pattern: re.Pattern[str] | None = None
        if item.get("match_regex"):
            try:
                pattern = re.compile(str(item["match_regex"]))
            except re.error as exc:
                raise LeakGuardError(f"{item_where}: invalid match_regex: {exc}") from exc
        literal = item.get("match")
        yield AllowEntry(
            id=entry_id,
            path_glob=_require_str(item.get("path_glob"), where=item_where, name="path_glob"),
            categories=_require_categories(item.get("categories"), where=item_where),
            justification=_require_justification(item.get("justification"), where=item_where),
            literal=str(literal) if literal is not None else None,
            pattern=pattern,
            remediation=str(item["remediation"]) if item.get("remediation") else None,
        )


def _load_exclusions(raw: Any, *, where: str) -> Iterator[ScopedExclusion]:
    if not raw:
        return
    if not isinstance(raw, list):
        raise LeakGuardError(f"{where}: scoped_exclusions must be a list")
    seen: set[str] = set()
    for index, item in enumerate(raw):
        item_where = f"{where}: scoped_exclusions[{index}]"
        if not isinstance(item, dict):
            raise LeakGuardError(f"{item_where}: entry must be a mapping")
        _unknown_keys(item, ("id", "path_glob", "categories", "justification"), where=item_where)
        exclusion_id = _require_str(item.get("id"), where=item_where, name="id")
        if exclusion_id in seen:
            raise LeakGuardError(f"{item_where}: duplicate exclusion id {exclusion_id!r}")
        seen.add(exclusion_id)
        yield ScopedExclusion(
            id=exclusion_id,
            path_glob=_require_str(item.get("path_glob"), where=item_where, name="path_glob"),
            categories=_require_categories(item.get("categories"), where=item_where),
            justification=_require_justification(item.get("justification"), where=item_where),
        )


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    column: int
    category: str
    rule: str
    match: str
    allowlisted_by: str | None = None

    @property
    def preview(self) -> str:
        return redact(self.match)

    def to_dict(self, *, show_matches: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "category": self.category,
            "rule": self.rule,
            "preview": self.match if show_matches else self.preview,
        }
        if self.allowlisted_by:
            payload["allowlisted_by"] = self.allowlisted_by
        return payload


def redact(text: str) -> str:
    """Keep enough of a match to locate it without republishing it."""

    if len(text) <= 4:
        return text[0] + "*" * (len(text) - 1)
    keep = 3 if len(text) > 8 else 2
    return f"{text[:keep]}{'*' * min(len(text) - keep, 12)} [{len(text)} chars]"


def _ip_allowed(value: str, policy: Policy) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True  # not an address at all
    networks = (
        policy.allowed_ipv4_networks if address.version == 4 else policy.allowed_ipv6_networks
    )
    return any(address in network for network in networks)


def _path_allowed(root: str, segment: str, policy: Policy) -> bool:
    candidate = f"{root}/{segment}"
    for prefix in policy.allowed_path_prefixes:
        normalized = prefix.rstrip("/")
        if candidate == normalized or candidate.startswith(normalized + "/"):
            return True
        if normalized.startswith(candidate + "/"):
            # A longer allowed prefix such as /home/weights/Qwen also allows the
            # shorter mount it lives under.
            return True
    return any(pattern.fullmatch(segment) for pattern in policy.allowed_path_segment_patterns)


def _is_placeholder_value(raw: str) -> bool:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
        quoted = True
    else:
        quoted = False
    value = value.strip()
    if not value or len(value) < 6:
        return True
    if any(char in value for char in "<>{}$\\ %"):
        return True
    if PLACEHOLDER_VALUE_RE.fullmatch(value):
        return True
    if value.endswith(("(", ")", ",", "+")):
        return True
    if not quoted and IDENTIFIER_VALUE_RE.fullmatch(value):
        # `token = invocation_id` and `password=password` are code, not secrets.
        return True
    if ENV_NAME_VALUE_RE.fullmatch(value):
        # `token_env = "VAWS_COORDINATOR_TOKEN"` names a variable, not a secret.
        return True
    if value.startswith(("/", "~/", "./", "../")):
        # `token_file = "/path/to/token"` points at a secret; it is not one.
        return True
    if SHORT_KEBAB_VALUE_RE.fullmatch(value):
        # Two-word kebab-case values are CLI modes and flag names in this repo
        # (`password-once`, `ssh-askpass`). Longer hyphenated values stay in
        # scope so a hyphenated passphrase is still reported.
        return True
    if len(set(value)) <= 2:
        return True
    if not quoted and not re.search(r"\d", value) and value.islower():
        # Bare lowercase words after `=` are almost always flags or fields.
        return True
    return False


def _hostname_in_scope(line: str, match: re.Match[str]) -> bool:
    """Separate DNS names from Python attribute access on a local variable.

    A quoted name, an ssh target, a URL host, and a `host:port` pair all carry
    a delimiter; a host label with a digit or hyphen, or a third label, carries
    the shape. Attribute access on a snake_case variable carries neither, and
    there is no way to read it as a hostname.
    """

    text = match.group(0)
    if text.count(".") >= 2 or HOST_SHAPED_LABEL_RE.match(text):
        return True
    left = line[match.start() - 1] if match.start() else ""
    right = line[match.end()] if match.end() < len(line) else ""
    if (left and left in HOSTNAME_LEFT_CONTEXT) or (right and right in HOSTNAME_RIGHT_CONTEXT):
        return True
    return bool(right == ":" and line[match.end() + 1 : match.end() + 2].isdigit())


_PACKAGE_SECRET_RULES = frozenset(
    {
        "credential-known-format",
        "credential-url-userinfo",
        "credential-bearer",
    }
)


def _secret_findings(line: str) -> Iterator[tuple[int, int, str, str]]:
    knowledge_redact = require_knowledge_redact()
    for hit in knowledge_redact.scan_text(line, allow=None, path="<line>"):
        if hit.rule not in _PACKAGE_SECRET_RULES:
            continue
        start = line.find(hit.value)
        end = start + len(hit.value) if start >= 0 else 0
        if start >= 0:
            yield start, end, "secret-value", hit.rule
    for pattern in EXTRA_SECRET_VALUE_RES:
        for match in pattern.finditer(line):
            yield match.start(), match.end(), "secret-value", "known-credential-format"
    for match in SECRET_ASSIGNMENT_RE.finditer(line):
        # Normalize the key to the underscore shape `SECRET_KEY_RE` expects, so
        # `api-key`, `apiKey["x"]`, and `spec.api_key` are all classified by the
        # same pattern the knowledge library already uses.
        normalized = WORD_SPLIT_RE.sub("_", match.group("key")).strip("_")
        if not SECRET_KEY_RE.search(f"_{normalized}_"):
            continue
        if _is_placeholder_value(match.group("value")):
            continue
        yield match.start("value"), match.end("value"), "secret-key", "secret-key-with-literal-value"


def scan_line(line: str, policy: Policy) -> list[tuple[int, int, str, str, str]]:
    """Return `(start, end, category, rule, text)` spans for one line."""

    require_knowledge_redact()
    raw: list[tuple[int, int, str, str, str]] = []

    for match in MAC_RE.finditer(line):
        text = match.group(0)
        normalized = text.lower().replace("-", ":")
        if any(normalized.startswith(prefix) for prefix in policy.allowed_mac_prefixes):
            continue
        raw.append((match.start(), match.end(), "mac-address", "mac-address", text))

    for match in IPV4_RE.finditer(line):
        text = match.group(0)
        try:
            ipaddress.IPv4Address(text)
        except ValueError:
            continue
        if _ip_allowed(text, policy):
            continue
        raw.append((match.start(), match.end(), "ipv4", "ipv4-address", text))

    for match in IPV6_RE.finditer(line):
        text = match.group(0)
        if text.count(":") < 2:
            continue
        bare = text.split("%", 1)[0]
        try:
            ipaddress.IPv6Address(bare)
        except ValueError:
            continue
        if _ip_allowed(bare, policy):
            continue
        raw.append((match.start(), match.end(), "ipv6", "ipv6-address", text))

    for match in policy.user_path_re.finditer(line):
        root, segment = match.group(1), match.group(2)
        if _path_allowed(root, segment, policy):
            continue
        raw.append(
            (match.start(), match.end(), "absolute-user-path", "home-directory-path", match.group(0))
        )

    for match in EMAIL_RE.finditer(line):
        text = match.group(0)
        domain = text.rsplit("@", 1)[1].lower()
        if domain in policy.allowed_email_domains:
            continue
        raw.append((match.start(), match.end(), "email", "email-address", text))

    for rule in policy.name_rules:
        for match in rule.pattern.finditer(line):
            text = match.group(0)
            if rule.category == "internal-hostname" and not _hostname_in_scope(line, match):
                continue
            if rule.category == "internal-identifier" and GIT_SHA_RE.fullmatch(text):
                # Short git SHAs are hex-only; identity tokens are not.
                continue
            raw.append((match.start(), match.end(), rule.category, rule.id, text))

    for start, end, category, rule_id in _secret_findings(line):
        raw.append((start, end, category, rule_id, line[start:end]))

    return _dedupe_spans(raw)


def _dedupe_spans(
    spans: list[tuple[int, int, str, str, str]]
) -> list[tuple[int, int, str, str, str]]:
    ordered = sorted(spans, key=lambda item: (CATEGORY_ORDER[item[2]], item[0], -item[1]))
    kept: list[tuple[int, int, str, str, str]] = []
    for span in ordered:
        if any(span[0] < other[1] and other[0] < span[1] for other in kept):
            continue
        kept.append(span)
    return sorted(kept, key=lambda item: item[0])


def scan_document(text: str, *, path: str, policy: Policy) -> list[Finding]:
    require_knowledge_redact()
    findings: list[Finding] = []
    exclusions = policy.scoped_categories(path)
    for number, line in enumerate(text.splitlines(), start=1):
        # Scan the complete line. File size is already bounded by max_file_bytes;
        # truncating here hid matches that staged diffs still reported.
        for start, _end, category, rule, matched in scan_line(line, policy):
            if any(item.excludes(category) for item in exclusions):
                continue
            entry = policy.allow_entry_for(path, category, matched)
            findings.append(
                Finding(
                    path=path,
                    line=number,
                    column=start + 1,
                    category=category,
                    rule=rule,
                    match=matched,
                    allowlisted_by=entry.id if entry else None,
                )
            )
    return findings


# --------------------------------------------------------------------------
# Git plumbing
# --------------------------------------------------------------------------


def run_git(args: Sequence[str], *, repo_root: Path, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise LeakGuardError(f"git {' '.join(args)} failed: {message}")
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def tracked_files(repo_root: Path) -> list[str]:
    """List tracked blobs, skipping gitlinks so submodules stay out of scope."""

    raw = run_git(["ls-files", "-s", "-z"], repo_root=repo_root)
    assert isinstance(raw, str)
    paths: list[str] = []
    for record in raw.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode = meta.split(" ", 1)[0]
        if mode == "160000":  # submodule gitlink; content is not ours to police
            continue
        paths.append(path)
    return sorted(paths)


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    suppressed: list[Finding] = field(default_factory=list)
    scanned: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)

    def record(self, findings: Iterable[Finding]) -> None:
        for finding in findings:
            if finding.allowlisted_by:
                self.suppressed.append(finding)
            else:
                self.findings.append(finding)


ProgressFn = Callable[[str], None]


def _noop(_message: str) -> None:
    return None


def scan_files(
    repo_root: Path,
    paths: Sequence[str],
    policy: Policy,
    *,
    progress: ProgressFn = _noop,
) -> ScanResult:
    require_knowledge_redact()
    result = ScanResult()
    total = len(paths)
    step = max(1, total // 8)
    for index, relative in enumerate(paths, start=1):
        if policy.is_path_excluded(relative):
            result.skipped.append({"path": relative, "reason": "excluded-by-policy"})
            continue
        absolute = repo_root / relative
        if absolute.is_symlink() or not absolute.is_file():
            result.skipped.append({"path": relative, "reason": "not-a-regular-file"})
            continue
        size = absolute.stat().st_size
        if size > policy.max_file_bytes:
            result.skipped.append({"path": relative, "reason": f"larger-than-{policy.max_file_bytes}-bytes"})
            continue
        data = absolute.read_bytes()
        if _is_binary(data):
            result.skipped.append({"path": relative, "reason": "binary"})
            continue
        result.scanned += 1
        result.record(scan_document(data.decode("utf-8", "replace"), path=relative, policy=policy))
        if index % step == 0 or index == total:
            progress(f"scanned {index}/{total} tracked files")
    return result


DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")

_GIT_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def unquote_git_path(text: str) -> str:
    """Decode a Git C-quoted pathname, or return `text` unchanged if unquoted."""

    if not text.startswith('"'):
        return text
    decoded, end = _unquote_git_c_style(text, 0)
    if end != len(text):
        raise LeakGuardError("malformed git-quoted path")
    return decoded


def _unquote_git_c_style(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != '"':
        raise LeakGuardError("malformed git-quoted path")
    raw = bytearray()
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == '"':
            return raw.decode("utf-8", "surrogateescape"), index + 1
        if char != "\\":
            raw.extend(char.encode("utf-8", "surrogateescape"))
            index += 1
            continue
        index += 1
        if index >= len(text):
            raise LeakGuardError("unterminated git-quoted path")
        esc = text[index]
        mapped = _GIT_C_ESCAPES.get(esc)
        if mapped is not None:
            raw.extend(mapped.encode("latin-1"))
            index += 1
            continue
        if esc in "01234567":
            value = 0
            digits = 0
            while digits < 3 and index < len(text) and text[index] in "01234567":
                value = (value << 3) | (ord(text[index]) - 48)
                index += 1
                digits += 1
            raw.append(value & 0xFF)
            continue
        raise LeakGuardError("invalid git-quoted path escape")
    raise LeakGuardError("unterminated git-quoted path")


def _parse_unified_file_header(line: str) -> str:
    """Return the path from a `---` or `+++` unified-diff header."""

    if line.startswith("+++ "):
        payload = line[4:]
        expect_prefix = "b/"
    elif line.startswith("--- "):
        payload = line[4:]
        expect_prefix = "a/"
    else:
        raise LeakGuardError("malformed diff file header")
    if payload.startswith('"'):
        path, end = _unquote_git_c_style(payload, 0)
        rest = payload[end:]
        if rest and not rest.startswith("\t"):
            raise LeakGuardError("malformed quoted diff path header")
    else:
        path = payload.split("\t", 1)[0]
    if path == "/dev/null":
        return path
    if not path.startswith(expect_prefix):
        raise LeakGuardError("unrecognized diff path header prefix")
    return path[len(expect_prefix) :]


def scan_diff(diff_text: str, policy: Policy) -> ScanResult:
    """Scan only added lines of a unified diff, keeping post-image line numbers.

    Path headers are decoded only in header state. Added hunk lines are scanned
    even when they begin with extra '+' characters, so a source line `++ host`
    is not mistaken for a `+++` path header.
    """

    require_knowledge_redact()
    result = ScanResult()
    path: str | None = None
    line_number = 0
    in_hunk = False
    files: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            in_hunk = False
            path = None
            continue
        if not in_hunk:
            if line.startswith("--- ") or line.startswith("+++ "):
                parsed = _parse_unified_file_header(line)
                if line.startswith("+++ "):
                    path = parsed
                continue
            hunk = DIFF_HUNK_RE.match(line)
            if hunk:
                if path is None:
                    raise LeakGuardError("diff hunk without a valid +++ path header")
                in_hunk = True
                line_number = int(hunk.group("start"))
            continue
        hunk = DIFF_HUNK_RE.match(line)
        if hunk:
            line_number = int(hunk.group("start"))
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            if path is None or path == "/dev/null":
                raise LeakGuardError("diff addition without a valid path header")
            if not policy.is_path_excluded(path):
                files.add(path)
                for start, _end, category, rule, matched in scan_line(line[1:], policy):
                    if any(item.excludes(category) for item in policy.scoped_categories(path)):
                        continue
                    entry = policy.allow_entry_for(path, category, matched)
                    result.record(
                        [
                            Finding(
                                path=path,
                                line=line_number,
                                column=start + 1,
                                category=category,
                                rule=rule,
                                match=matched,
                                allowlisted_by=entry.id if entry else None,
                            )
                        ]
                    )
            line_number += 1
            continue
        if line.startswith("-"):
            continue
        if line.startswith(" ") or not line:
            line_number += 1
    result.scanned = len(files)
    return result


def staged_diff(repo_root: Path) -> str:
    raw = run_git(
        ["diff", "--cached", "--unified=0", "--no-color", "--diff-filter=ACMR"],
        repo_root=repo_root,
    )
    assert isinstance(raw, str)
    return raw


def range_diff(repo_root: Path, commit_range: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_./^~@{}-]{1,200}", commit_range):
        raise LeakGuardError(f"unsafe commit range: {commit_range!r}")
    raw = run_git(
        ["diff", "--unified=0", "--no-color", "--diff-filter=ACMR", commit_range],
        repo_root=repo_root,
    )
    assert isinstance(raw, str)
    return raw


def unused_entry_ids(policy: Policy) -> list[str]:
    return sorted({entry.id for entry in policy.entries} - policy.used_entry_ids)


def format_finding_line(finding: Finding, *, show_matches: bool) -> str:
    text = finding.match if show_matches else finding.preview
    return f"{finding.path}:{finding.line}:{finding.column}: {finding.category} ({finding.rule}): {text}"
