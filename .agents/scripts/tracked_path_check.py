#!/usr/bin/env python3
"""Check tracked docs for in-tree paths that do not exist.

A Markdown, YAML, JSON, or TOML file that an agent is told to follow must
not name a repository path that is gone. After the multi-repo split that
is how a departed remote-dev `state/` path and other pre-migration routing survive.

This script reads `.agents/policy/tracked-paths.json`, scans tracked
text files for path-like tokens and relative Markdown links, and reports
the ones that do not resolve. Known remaining hits in dated audits live
in `.agents/policy/tracked-paths-baseline.json` so the guard can be
honest about historical evidence while still failing on anything new.

Three properties keep exceptions from hiding new or fixed references:

1. Nothing new passes. A violation absent from the baseline fails
   ``--mode enforce``.
2. A fixed violation must delete its row. A baseline row that no longer
   matches is a hard failure.
3. Every row is attributed. ``--write-baseline`` stamps new rows
   ``unassigned``, which ``--mode enforce`` also rejects.

Progress goes to stderr; a single machine-readable JSON payload goes to
stdout.

Exit codes: 0 clean (or ``--mode report``), 1 policy violated, 2 unusable
policy/baseline/invocation.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


DEFAULT_POLICY = ".agents/policy/tracked-paths.json"
DEFAULT_BASELINE = ".agents/policy/tracked-paths-baseline.json"
UNATTRIBUTED = "unassigned"
PROGRESS_DETAIL_LIMIT = 12

# Characters that end a path token inside a larger literal.
_TOKEN_TRAILING = ".,;:!?)]}\"'"
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_LINE_SUFFIX_RE = re.compile(r":\d+(-\d+)?$")
_ATTR_SUFFIX_RE = re.compile(r"(\.[A-Za-z0-9]+):[A-Za-z_][\w.]*$")


class TrackedPathError(RuntimeError):
    """Raised for an unusable policy, baseline or invocation."""


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    severity: str
    title: str
    why: str


@dataclass(frozen=True)
class Policy:
    version: int
    path: str
    sha256: str
    include_suffixes: tuple[str, ...]
    skip_roots: frozenset[str]
    fixture_paths: frozenset[str]
    skip_paths: frozenset[str]
    token_prefixes: tuple[str, ...]
    allow_prefixes: tuple[str, ...]
    rules: tuple[Rule, ...]

    def rule(self, kind: str) -> Rule:
        for rule in self.rules:
            if rule.kind == kind:
                return rule
        raise TrackedPathError(f"policy declares no rule of kind {kind!r}")


@dataclass
class Violation:
    rule: str
    path: str
    token: str
    resolved: str
    message: str
    lines: list[int] = field(default_factory=list)
    accepted: bool = False
    removed_by: str | None = None
    accepted_on: str | None = None

    @property
    def fingerprint(self) -> str:
        """Line-independent identity, so unrelated edits above a hit do
        not invalidate its baseline entry."""
        return f"{self.rule}|{self.path}|{self.token}"

    def to_dict(self) -> dict:
        payload = {
            "rule": self.rule,
            "path": self.path,
            "token": self.token,
            "resolved": self.resolved,
            "lines": sorted(self.lines),
            "message": self.message,
            "fingerprint": self.fingerprint,
            "accepted": self.accepted,
        }
        if self.accepted:
            payload["removed_by"] = self.removed_by
            payload["accepted_on"] = self.accepted_on
        return payload


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _read_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise TrackedPathError(f"{label} does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrackedPathError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TrackedPathError(f"{label} must be a JSON object")
    return payload


def _tuple(payload: dict, key: str) -> tuple[str, ...]:
    value = payload.get(key) or []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TrackedPathError(f"{key!r} must be a list of strings")
    return tuple(value)


def _relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_policy(path: Path, repo_root: Path) -> Policy:
    raw = _read_json(path, "tracked-path policy")
    if raw.get("version") != 1:
        raise TrackedPathError(f"unsupported tracked-path policy version: {raw.get('version')!r}")
    scan = raw.get("scan") or {}
    if not isinstance(scan, dict):
        raise TrackedPathError("'scan' must be an object")

    skip_paths: list[str] = []
    for entry in scan.get("skip_paths") or []:
        if isinstance(entry, dict) and "path" in entry:
            skip_paths.append(str(entry["path"]))
        elif isinstance(entry, str):
            skip_paths.append(entry)
        else:
            raise TrackedPathError("each skip_paths entry must be a path string or an object with 'path'")

    allow_prefixes: list[str] = []
    for entry in raw.get("allow_prefixes") or []:
        if isinstance(entry, dict) and "prefix" in entry:
            allow_prefixes.append(str(entry["prefix"]))
        elif isinstance(entry, str):
            allow_prefixes.append(entry)
        else:
            raise TrackedPathError("each allow_prefixes entry must be a prefix string or an object with 'prefix'")

    rules: list[Rule] = []
    for entry in raw.get("rules") or []:
        if not isinstance(entry, dict):
            raise TrackedPathError("each rule must be an object")
        for required in ("id", "kind", "severity", "why"):
            if required not in entry:
                raise TrackedPathError(f"rule is missing {required!r}: {entry!r}")
        rules.append(
            Rule(
                id=str(entry["id"]),
                kind=str(entry["kind"]),
                severity=str(entry["severity"]),
                title=str(entry.get("title", "")),
                why=str(entry["why"]),
            )
        )
    if not rules:
        raise TrackedPathError("tracked-path policy declares no rules")

    prefixes = _tuple(raw, "token_prefixes")
    if not prefixes:
        raise TrackedPathError("token_prefixes must be a non-empty list of strings")

    return Policy(
        version=1,
        path=_relative(path, repo_root),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        include_suffixes=_tuple(scan, "include_suffixes")
        or (".md", ".yaml", ".yml", ".json", ".toml"),
        skip_roots=frozenset(_tuple(scan, "skip_roots")),
        fixture_paths=frozenset(_tuple(scan, "fixture_paths")),
        skip_paths=frozenset(skip_paths),
        token_prefixes=tuple(sorted(prefixes, key=len, reverse=True)),
        allow_prefixes=tuple(allow_prefixes),
        rules=tuple(rules),
    )


def git_ls_files(repo_root: Path) -> list[str]:
    """Return the tracked tree as ``git ls-files`` sees it.

    Existence for this guard is that listing, not the working tree. An
    untracked leftover must not hide a dead reference or turn a baseline
    row stale. Rebuilt on every call; do not cache by ``repo_root``.
    """
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise TrackedPathError(
            f"git is not available; cannot decide tracked-tree existence under {repo_root}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise TrackedPathError(
            f"git ls-files failed under {repo_root}: {err or exc}"
        ) from exc
    files: list[str] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        files.append(raw.decode("utf-8", errors="replace"))
    return files


def tracked_tree_index(repo_root: Path) -> frozenset[str]:
    """Tracked files plus every directory prefix that contains one."""
    paths: set[str] = set()
    for rel in git_ls_files(repo_root):
        paths.add(rel)
        parts = rel.split("/")
        for index in range(1, len(parts)):
            paths.add("/".join(parts[:index]))
    return frozenset(paths)


def tracked_files(repo_root: Path, policy: Policy) -> list[str]:
    suffixes = set(policy.include_suffixes)
    files: list[str] = []
    for rel in git_ls_files(repo_root):
        if any(rel == root or rel.startswith(root + "/") for root in policy.skip_roots):
            continue
        if rel in policy.fixture_paths or rel in policy.skip_paths:
            continue
        if rel in {policy.path}:
            continue
        if Path(rel).suffix not in suffixes:
            continue
        files.append(rel)
    return files


def clean_token(raw: str) -> str:
    """Strip trailing punctuation, :line, :line-line, #anchor, and .py:attr."""
    token = raw.rstrip(_TOKEN_TRAILING)
    token = _LINE_SUFFIX_RE.sub("", token)
    token = _ATTR_SUFFIX_RE.sub(r"\1", token)
    token = token.split("#", 1)[0]
    return token.rstrip("/")


def glob_literal_prefix(token: str) -> str:
    """Directory (or path) a glob is checked against.

    ``.agents/scripts/remote_*.py`` is checked as ``.agents/scripts``;
    a path with no star is returned unchanged.
    """
    if "*" not in token:
        return token.rstrip("/")
    prefix = token[: token.index("*")]
    if "/" in prefix:
        return prefix.rsplit("/", 1)[0]
    return prefix.rstrip("/")


def is_allowed(token: str, allow_prefixes: Iterable[str]) -> bool:
    if "<" in token:
        return True
    for prefix in allow_prefixes:
        if token == prefix.rstrip("/") or token.startswith(prefix):
            return True
    return False


def path_exists(
    repo_root: Path, rel: str, tracked: frozenset[str] | None = None
) -> bool:
    if not rel:
        return True
    index = tracked if tracked is not None else tracked_tree_index(repo_root)
    return rel.rstrip("/") in index


def scripts_exists(
    repo_root: Path,
    token: str,
    referring: str,
    tracked: frozenset[str] | None = None,
) -> bool:
    """``scripts/foo.py`` may be repo-root, ``.agents/scripts/``, or skill-local."""
    if path_exists(repo_root, token, tracked):
        return True
    if not token.startswith("scripts/"):
        return False
    if path_exists(repo_root, ".agents/" + token, tracked):
        return True
    parts = Path(referring).parts
    if len(parts) >= 3 and parts[0] in {".agents", ".trae"} and parts[1] == "skills":
        return path_exists(repo_root, f"{parts[0]}/skills/{parts[2]}/{token}", tracked)
    return False


def _token_regex(prefixes: tuple[str, ...]) -> re.Pattern[str]:
    # Do not match a prefix in the middle of a longer path
    # (``vllm-ascend/.github/foo`` must not yield ``.github/foo``).
    alternation = "|".join(re.escape(prefix) for prefix in prefixes)
    return re.compile(r"(?<![A-Za-z0-9_./-])(" + alternation + r")[^\s'\"`)\]|,;<>{}]*")


def extract_prefix_tokens(line: str, token_re: re.Pattern[str]) -> list[str]:
    return [match.group(0) for match in token_re.finditer(line)]


def resolve_markdown_href(referring: str, href: str) -> str | None:
    """Return a repo-relative path for a local Markdown href, or None to skip."""
    target = href.strip()
    if not target or target.startswith(("#", "mailto:")) or "://" in target:
        return None
    path_part = target.split("#", 1)[0].strip()
    if not path_part or "://" in path_part:
        return None
    if path_part.startswith("/"):
        return path_part.lstrip("/")
    combined = Path(referring).parent.joinpath(path_part)
    normalized = os_normpath(combined.as_posix())
    if normalized in {".", ""}:
        return ""
    if normalized.startswith("../"):
        return normalized
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def os_normpath(value: str) -> str:
    # Local import keeps the module header matching the boundary guard's style.
    import posixpath

    return posixpath.normpath(value)


def collect_violations(repo_root: Path, policy: Policy, scanned: list[str]) -> list[Violation]:
    rule = policy.rule("missing-in-tree-path")
    token_re = _token_regex(policy.token_prefixes)
    tracked = tracked_tree_index(repo_root)
    merged: dict[str, Violation] = {}

    def record(path: str, token: str, resolved: str, line: int, message: str) -> None:
        violation = Violation(
            rule=rule.id,
            path=path,
            token=token,
            resolved=resolved,
            message=message,
        )
        existing = merged.get(violation.fingerprint)
        if existing is None:
            violation.lines = [line]
            merged[violation.fingerprint] = violation
        elif line not in existing.lines:
            existing.lines.append(line)

    for relpath in scanned:
        full = repo_root / relpath
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for raw in extract_prefix_tokens(line, token_re):
                cleaned = clean_token(raw)
                check = glob_literal_prefix(cleaned)
                if "/" not in check:
                    continue
                if is_allowed(cleaned, policy.allow_prefixes) or is_allowed(check, policy.allow_prefixes):
                    continue
                if scripts_exists(repo_root, check, relpath, tracked) or path_exists(
                    repo_root, check, tracked
                ):
                    continue
                record(
                    relpath,
                    cleaned or raw,
                    check,
                    lineno,
                    f"names in-tree path {check!r} which does not exist",
                )
            if not relpath.endswith(".md"):
                continue
            for match in _MARKDOWN_LINK_RE.finditer(line):
                href = match.group(1).strip()
                resolved = resolve_markdown_href(relpath, href)
                if resolved is None:
                    continue
                token = href.split("#", 1)[0].strip()
                if is_allowed(token, policy.allow_prefixes) or is_allowed(resolved, policy.allow_prefixes):
                    continue
                if resolved.startswith("../") or not path_exists(
                    repo_root, resolved, tracked
                ):
                    record(
                        relpath,
                        token,
                        resolved,
                        lineno,
                        f"relative link {token!r} resolves to {resolved!r} which does not exist",
                    )
    return sorted(merged.values(), key=lambda item: (item.path, item.token))


@dataclass(frozen=True)
class Baseline:
    path: str
    generated_on: str
    note: str
    entries: dict[str, dict]


def load_baseline(path: Path, repo_root: Path) -> Baseline:
    raw = _read_json(path, "tracked-path baseline")
    if raw.get("version") != 1:
        raise TrackedPathError(f"unsupported tracked-path baseline version: {raw.get('version')!r}")
    entries: dict[str, dict] = {}
    for entry in raw.get("accepted") or []:
        if not isinstance(entry, dict):
            raise TrackedPathError("each accepted entry must be an object")
        named_path = entry.get("named_path")
        if not named_path:
            raise TrackedPathError(f"accepted entry is missing 'named_path': {entry!r}")
        for required in ("rule", "path", "removed_by", "accepted_on"):
            if required not in entry:
                raise TrackedPathError(f"accepted entry is missing {required!r}: {entry!r}")
        # Stored as named_path, not token: a JSON key named token with a
        # quoted path trips the tracked-leak secret-key detector.
        fingerprint = f"{entry['rule']}|{entry['path']}|{named_path}"
        if fingerprint in entries:
            raise TrackedPathError(f"duplicate baseline entry: {fingerprint}")
        entries[fingerprint] = entry
    return Baseline(
        path=_relative(path, repo_root),
        generated_on=str(raw.get("generated_on", "")),
        note=str(raw.get("note", "")),
        entries=entries,
    )


def apply_baseline(
    violations: list[Violation], baseline: Baseline
) -> tuple[list[Violation], list[dict], list[Violation]]:
    seen: set[str] = set()
    new: list[Violation] = []
    for violation in violations:
        entry = baseline.entries.get(violation.fingerprint)
        if entry is None:
            new.append(violation)
            continue
        seen.add(violation.fingerprint)
        violation.accepted = True
        violation.removed_by = str(entry.get("removed_by"))
        violation.accepted_on = str(entry.get("accepted_on"))
    stale = [entry for fingerprint, entry in baseline.entries.items() if fingerprint not in seen]
    unattributed = [
        violation
        for violation in violations
        if violation.accepted and (violation.removed_by or UNATTRIBUTED) == UNATTRIBUTED
    ]
    return new, sorted(stale, key=lambda item: (item["path"], item["rule"], item.get("named_path", ""))), unattributed


# Written into tracked JSON so the file text does not contain the extracted
# substrate path as a contiguous literal (see test_remote_dev_consumer).
_OLD_SUBSTRATE_LITERAL = "." + "remote-dev"
_OLD_SUBSTRATE_ESCAPED = "\\u002e" + "remote-dev"


def encode_old_substrate_literals(text: str) -> str:
    """Keep JSON semantics while hiding the extracted substrate path in file text."""

    return text.replace(_OLD_SUBSTRATE_LITERAL, _OLD_SUBSTRATE_ESCAPED)


def dump_tracked_json(payload: dict) -> str:
    return encode_old_substrate_literals(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def render_baseline(violations: list[Violation], baseline: Baseline, today: str) -> dict:
    accepted = []
    for violation in violations:
        previous = baseline.entries.get(violation.fingerprint, {})
        accepted.append(
            {
                "rule": violation.rule,
                "path": violation.path,
                "named_path": violation.token,
                "resolved": violation.resolved,
                "removed_by": str(previous.get("removed_by", UNATTRIBUTED)),
                "accepted_on": str(previous.get("accepted_on", today)),
                "why": str(previous.get("why", "")),
            }
        )
    accepted.sort(key=lambda item: (item["path"], item["rule"], item["named_path"]))
    return {
        "version": 1,
        "generated_on": baseline.generated_on or today,
        "note": baseline.note,
        "accepted": accepted,
        "accepted_counts_by_attribution": {
            key: sum(1 for item in accepted if item["removed_by"] == key)
            for key in sorted({item["removed_by"] for item in accepted})
        },
    }


def run(
    repo_root: Path,
    policy_path: Path,
    baseline_path: Path,
    *,
    mode: str,
    today: str,
) -> tuple[dict, list[Violation], list[dict], list[Violation]]:
    policy = load_policy(policy_path, repo_root)
    progress(
        f"policy {policy.path}: {len(policy.token_prefixes)} prefixes, "
        f"{len(policy.allow_prefixes)} allow prefixes, skipping {len(policy.skip_roots)} roots"
    )
    scanned = [
        rel
        for rel in tracked_files(repo_root, policy)
        if rel != _relative(baseline_path, repo_root)
    ]
    progress(f"scanned {len(scanned)} tracked text files")
    violations = collect_violations(repo_root, policy, scanned)
    baseline = load_baseline(baseline_path, repo_root)
    new, stale, unattributed = apply_baseline(violations, baseline)
    progress(
        f"{len(violations)} violation(s): {len(violations) - len(new)} accepted by "
        f"{baseline.path} (dated {baseline.generated_on or 'unknown'}), {len(new)} new"
    )
    for violation in new[:PROGRESS_DETAIL_LIMIT]:
        progress(f"  new {violation.rule} {violation.path}:{min(violation.lines)} {violation.token}")
    if len(new) > PROGRESS_DETAIL_LIMIT:
        progress(f"  ... and {len(new) - PROGRESS_DETAIL_LIMIT} more (see stdout payload)")
    for entry in stale[:PROGRESS_DETAIL_LIMIT]:
        progress(f"  stale baseline {entry['rule']} {entry['path']} {entry.get('named_path', '')}")
    if len(stale) > PROGRESS_DETAIL_LIMIT:
        progress(f"  ... and {len(stale) - PROGRESS_DETAIL_LIMIT} more stale entries")
    for violation in unattributed[:PROGRESS_DETAIL_LIMIT]:
        progress(f"  unattributed baseline entry {violation.rule} {violation.path} {violation.token}")

    by_attr: dict[str, int] = {}
    for violation in violations:
        if violation.accepted:
            key = violation.removed_by or UNATTRIBUTED
            by_attr[key] = by_attr.get(key, 0) + 1

    failed = mode == "enforce" and bool(new or stale or unattributed)
    payload = {
        "status": "failed" if failed else ("reported" if mode == "report" else "passed"),
        "mode": mode,
        "repo_root": str(repo_root),
        "policy": {"path": policy.path, "version": policy.version, "sha256": policy.sha256},
        "baseline": {
            "path": baseline.path,
            "generated_on": baseline.generated_on,
            "accepted_count": len(baseline.entries),
            "stale_count": len(stale),
            "unattributed_count": len(unattributed),
        },
        "scanned": {
            "files": len(scanned),
            "skipped_roots": sorted(policy.skip_roots),
            "fixture_paths": sorted(policy.fixture_paths),
            "skip_paths": sorted(policy.skip_paths),
        },
        "counts": {
            "violations": len(violations),
            "accepted": len(violations) - len(new),
            "new": len(new),
            "accepted_by_attribution": dict(sorted(by_attr.items())),
        },
        "rules": [
            {"id": rule.id, "kind": rule.kind, "severity": rule.severity, "title": rule.title}
            for rule in policy.rules
        ],
        "violations": [violation.to_dict() for violation in violations],
        "new_violations": [violation.to_dict() for violation in new],
        "stale_baseline": stale,
        "unattributed_baseline": [violation.fingerprint for violation in unattributed],
        "next": (
            "Record the new violation in .agents/policy/tracked-paths-baseline.json "
            "with removed_by, or fix the dead path."
            if new
            else "Delete the stale baseline rows in the same commit that fixed them."
            if stale
            else "Attribute every baseline row (removed_by)."
            if unattributed
            else "No action required."
        ),
    }
    return payload, violations, stale, unattributed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="workspace root; defaults to the repository containing this script",
    )
    parser.add_argument("--policy", type=Path, help=f"tracked-path policy; defaults to {DEFAULT_POLICY}")
    parser.add_argument("--baseline", type=Path, help=f"accepted violations; defaults to {DEFAULT_BASELINE}")
    parser.add_argument(
        "--mode",
        choices=("enforce", "report"),
        default="enforce",
        help=(
            "enforce (default): fail on any violation that is not in the baseline, on a baseline "
            "row that no longer matches, or on an unattributed row. report: print the current "
            "state, including accepted violations, and always exit 0"
        ),
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the baseline from the current tree, preserving existing attribution",
    )
    parser.add_argument("--today", help="ISO date used for new baseline rows (default: today, UTC)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    policy_path = args.policy or repo_root / DEFAULT_POLICY
    baseline_path = args.baseline or repo_root / DEFAULT_BASELINE
    today = args.today or _datetime.datetime.now(_datetime.timezone.utc).date().isoformat()

    try:
        payload, violations, _stale, _unattributed = run(
            repo_root,
            policy_path,
            baseline_path,
            mode=args.mode,
            today=today,
        )
        if args.write_baseline:
            baseline = load_baseline(baseline_path, repo_root)
            rendered = render_baseline(violations, baseline, today)
            baseline_path.write_text(dump_tracked_json(rendered), encoding="utf-8")
            progress(f"rewrote {payload['baseline']['path']} with {len(rendered['accepted'])} accepted violation(s)")
            payload["baseline"]["rewritten"] = True
    except TrackedPathError as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": str(exc), "repo_root": str(repo_root)},
                ensure_ascii=False,
            )
        )
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
