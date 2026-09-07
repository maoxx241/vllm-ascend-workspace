#!/usr/bin/env python3
"""Check the repository's cross-subsystem dependency direction.

`vllm-ascend-workspace` is being split into a multi-repo organization. Three
subsystems leave this tree: the remote developer substrate (`.remote-dev/` ->
`vllm-ascend-workspace/remote-dev`), the coordinator (`.agents/coordinator/` ->
`vllm-ascend-workspace/vaws-coordinator`), and the fleet dashboard
(`vllm-ascend-workspace/vaws-top`). Everything the domain layer consumes from
them becomes a cross-repo dependency, and everything they consume from the
domain layer becomes an extraction blocker.

This script reads the declared boundary policy
(`.agents/policy/repo-boundaries.json`), scans tracked Python for references
that cross a subsystem boundary, and reports the ones the policy forbids. The
known violations of today live in a dated baseline
(`.agents/policy/repo-boundaries-baseline.json`) so the guard can be honest
about the current tree while still failing on anything new.

Detection is AST-based, not textual:

* imports are read from `ast.Import` / `ast.ImportFrom` at any nesting depth,
  so a lazy import inside a function body is found even though no textual
  search of the call site would show it;
* path references are read from string constants with module, class and
  function docstrings excluded, so a docstring or comment that merely mentions
  another subsystem is not reported as a dependency.

Progress goes to stderr; a single machine-readable JSON payload goes to stdout.

Exit codes: 0 clean (or `--mode report`), 1 policy violated, 2 bad invocation.
"""

from __future__ import annotations

import argparse
import ast
import datetime as _datetime
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

DEFAULT_POLICY = ".agents/policy/repo-boundaries.json"
DEFAULT_BASELINE = ".agents/policy/repo-boundaries-baseline.json"
UNATTRIBUTED = "unassigned"
PROGRESS_DETAIL_LIMIT = 12

# A path literal counts as a reference when the subsystem root is followed by a
# path separator (`.agents/lib/...`) or when the literal is exactly the root
# (`".agents"`, as handed to a subprocess argument list). Prose that merely
# names a root without descending into it is not a reference.
#
# Characters that end a path token inside a larger literal, so a shell string
# like "python3 .agents/skills/x/scripts/y.py --flag" yields just the script.
_TOKEN_TERMINATORS = set(" \t\n\r'\"`),;|<>{}")
_TOKEN_TRAILING = "/.,:;"


class BoundaryError(RuntimeError):
    """Raised for an unusable policy, baseline or invocation."""


@dataclass(frozen=True)
class Subsystem:
    id: str
    repo: str
    layer: int
    summary: str
    roots: tuple[str, ...]
    public_paths: tuple[str, ...]
    private_paths: tuple[str, ...]
    public_modules: tuple[str, ...]
    inline_patterns: tuple[str, ...]


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
    module_search_paths: tuple[str, ...]
    ignored_modules: frozenset[str]
    subsystems: tuple[Subsystem, ...]
    rules: tuple[Rule, ...]

    def rule(self, kind: str) -> Rule:
        for rule in self.rules:
            if rule.kind == kind:
                return rule
        raise BoundaryError(f"policy declares no rule of kind {kind!r}")

    def subsystem(self, subsystem_id: str) -> Subsystem:
        for subsystem in self.subsystems:
            if subsystem.id == subsystem_id:
                return subsystem
        raise BoundaryError(f"policy declares no subsystem {subsystem_id!r}")


@dataclass(frozen=True)
class Reference:
    """One boundary-crossing reference found in one file."""

    path: str
    line: int
    kind: str  # "import" | "path-literal"
    symbol: str
    source: str  # source subsystem id
    target: str  # target subsystem id
    detail: str


@dataclass
class Violation:
    rule: str
    severity: str
    path: str
    symbol: str
    kind: str
    source_subsystem: str
    target_subsystem: str
    message: str
    lines: list[int] = field(default_factory=list)
    accepted: bool = False
    removed_by: str | None = None
    accepted_on: str | None = None

    @property
    def fingerprint(self) -> str:
        """Line-independent identity, so unrelated edits above a violation do
        not invalidate its baseline entry."""
        return f"{self.rule}|{self.path}|{self.symbol}"

    def to_dict(self) -> dict:
        payload = {
            "rule": self.rule,
            "severity": self.severity,
            "path": self.path,
            "lines": sorted(self.lines),
            "kind": self.kind,
            "symbol": self.symbol,
            "from": self.source_subsystem,
            "to": self.target_subsystem,
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


# ---------------------------------------------------------------------------
# policy loading
# ---------------------------------------------------------------------------


def _read_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise BoundaryError(f"{label} does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BoundaryError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BoundaryError(f"{label} must be a JSON object")
    return payload


def _tuple(payload: dict, key: str) -> tuple[str, ...]:
    value = payload.get(key) or []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BoundaryError(f"{key!r} must be a list of strings")
    return tuple(value)


def load_policy(path: Path, repo_root: Path) -> Policy:
    raw = _read_json(path, "boundary policy")
    if raw.get("version") != 1:
        raise BoundaryError(f"unsupported boundary policy version: {raw.get('version')!r}")
    scan = raw.get("scan") or {}
    if not isinstance(scan, dict):
        raise BoundaryError("'scan' must be an object")

    subsystems: list[Subsystem] = []
    for entry in raw.get("subsystems") or []:
        if not isinstance(entry, dict):
            raise BoundaryError("each subsystem must be an object")
        for required in ("id", "repo", "layer"):
            if required not in entry:
                raise BoundaryError(f"subsystem is missing {required!r}: {entry!r}")
        if not isinstance(entry["layer"], int):
            raise BoundaryError(f"subsystem {entry['id']!r} layer must be an integer")
        subsystems.append(
            Subsystem(
                id=str(entry["id"]),
                repo=str(entry["repo"]),
                layer=int(entry["layer"]),
                summary=str(entry.get("summary", "")),
                roots=_tuple(entry, "roots"),
                public_paths=_tuple(entry, "public_paths"),
                private_paths=_tuple(entry, "private_paths"),
                public_modules=_tuple(entry, "public_modules"),
                inline_patterns=_tuple(entry, "inline_patterns"),
            )
        )
    if not subsystems:
        raise BoundaryError("boundary policy declares no subsystems")
    identifiers = [subsystem.id for subsystem in subsystems]
    if len(set(identifiers)) != len(identifiers):
        raise BoundaryError("subsystem ids must be unique")

    rules: list[Rule] = []
    for entry in raw.get("rules") or []:
        if not isinstance(entry, dict):
            raise BoundaryError("each rule must be an object")
        for required in ("id", "kind", "severity", "why"):
            if required not in entry:
                raise BoundaryError(f"rule is missing {required!r}: {entry!r}")
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
        raise BoundaryError("boundary policy declares no rules")

    ignored = {
        str(item["module"])
        for item in raw.get("ignored_modules") or []
        if isinstance(item, dict) and "module" in item
    }
    return Policy(
        version=1,
        path=_relative(path, repo_root),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        include_suffixes=_tuple(scan, "include_suffixes") or (".py",),
        skip_roots=frozenset(_tuple(scan, "skip_roots")),
        fixture_paths=frozenset(_tuple(scan, "fixture_paths")),
        module_search_paths=_tuple(scan, "module_search_paths"),
        ignored_modules=frozenset(ignored),
        subsystems=tuple(subsystems),
        rules=tuple(rules),
    )


def _relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# ownership resolution
# ---------------------------------------------------------------------------


def _prefix_depth(candidate: str, relpath: str) -> int | None:
    """Return how many path components of `candidate` match `relpath`, or None.

    `"."` is the repository root and matches everything at depth 0, so it acts
    as the fallback owner without out-ranking a more specific root.
    """
    if candidate in {"", "."}:
        return 0
    if relpath == candidate:
        return len(candidate.split("/"))
    if relpath.startswith(candidate + "/"):
        return len(candidate.split("/"))
    return None


class Ownership:
    """Maps repository paths and module names to owning subsystems."""

    def __init__(self, policy: Policy, repo_root: Path) -> None:
        self.policy = policy
        self.repo_root = repo_root
        self._roots: list[tuple[int, str, Subsystem]] = []
        for subsystem in policy.subsystems:
            for root in subsystem.roots:
                self._roots.append((len(root.split("/")) if root != "." else 0, root, subsystem))
        # Longest root wins, so `.agents/coordinator` out-ranks `.agents`.
        self._roots.sort(key=lambda item: item[0], reverse=True)
        self._modules = self._derive_modules()

    def subsystem_for_path(self, relpath: str) -> Subsystem | None:
        best: tuple[int, Subsystem] | None = None
        for _depth, root, subsystem in self._roots:
            matched = _prefix_depth(root, relpath)
            if matched is None:
                continue
            if best is None or matched > best[0]:
                best = (matched, subsystem)
        return best[1] if best else None

    def _derive_modules(self) -> dict[str, Subsystem]:
        """Derive importable top-level module names from the directories that
        an entry point actually places on sys.path.

        Ownership comes from the *file's* path, so a module follows its file
        when the file moves across the boundary; the policy never restates it.
        """
        modules: dict[str, Subsystem] = {}
        for search in self.policy.module_search_paths:
            directory = self.repo_root / search
            if not directory.is_dir():
                continue
            for child in sorted(directory.iterdir()):
                if child.name.startswith("__") or child.name in self.policy.skip_roots:
                    continue
                if child.is_file() and child.suffix == ".py":
                    name = child.stem
                elif child.is_dir() and (child / "__init__.py").is_file():
                    name = child.name
                else:
                    continue
                if name in self.policy.ignored_modules:
                    continue
                owner = self.subsystem_for_path(_relative(child, self.repo_root))
                if owner is not None:
                    modules.setdefault(name, owner)
        return modules

    def subsystem_for_module(self, dotted: str) -> Subsystem | None:
        head = dotted.split(".", 1)[0]
        if head in self.policy.ignored_modules:
            return None
        return self._modules.get(head)

    @property
    def module_count(self) -> int:
        return len(self._modules)


def is_published_path(subsystem: Subsystem, relpath: str) -> bool:
    """Longest-prefix vote between the subsystem's public and private paths.

    Anything the policy does not explicitly publish is private, so adding a new
    internal directory does not silently widen the published surface.
    """
    best_public = max(
        (depth for depth in (_prefix_depth(item, relpath) for item in subsystem.public_paths) if depth is not None),
        default=-1,
    )
    best_private = max(
        (depth for depth in (_prefix_depth(item, relpath) for item in subsystem.private_paths) if depth is not None),
        default=-1,
    )
    return best_public > best_private


def is_published_module(subsystem: Subsystem, dotted: str) -> bool:
    return any(dotted == item or dotted.startswith(item + ".") for item in subsystem.public_modules)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def iter_source_files(repo_root: Path, policy: Policy) -> Iterator[Path]:
    suffixes = set(policy.include_suffixes)

    def walk(directory: Path) -> Iterator[Path]:
        for child in sorted(directory.iterdir()):
            if child.name in policy.skip_roots:
                continue
            if child.is_symlink():
                continue
            if child.is_dir():
                yield from walk(child)
            elif child.suffix in suffixes:
                yield child

    yield from walk(repo_root)


def _docstring_constants(tree: ast.Module) -> set[int]:
    """Object ids of every module/class/function docstring constant."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            ids.add(id(first.value))
    return ids


def _string_constants(tree: ast.Module) -> Iterator[tuple[int, str]]:
    skip = _docstring_constants(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.lineno, node.value


def _imported_names(tree: ast.Module) -> Iterator[tuple[int, str, str]]:
    """(line, dotted module, reported symbol) for every absolute import."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue  # relative imports never cross a subsystem boundary
            for alias in node.names:
                yield node.lineno, node.module, f"{node.module}.{alias.name}"


def _literal_path_references(literal: str, roots: Sequence[str]) -> list[str]:
    """Path tokens in a string literal that descend into a subsystem root.

    Returns the referenced paths, not the roots, so that a shell command
    embedded in a literal reports the script it actually invokes. Longest
    token first.
    """
    tokens: set[str] = set()
    stripped = literal.strip()
    for root in roots:
        if root in {"", "."}:
            continue
        if stripped == root:
            tokens.add(root)
            continue
        index = 0
        while True:
            index = literal.find(root, index)
            if index < 0:
                break
            if literal[index + len(root): index + len(root) + 1] == "/":
                token = literal[index:]
                for cut, char in enumerate(token):
                    if char in _TOKEN_TERMINATORS:
                        token = token[:cut]
                        break
                token = token.rstrip(_TOKEN_TRAILING)
                if token:
                    tokens.add(token)
                break
            index += 1
    return sorted(tokens, key=len, reverse=True)


def _inline_hits(literal: str, patterns: Sequence[str]) -> list[str]:
    stripped = literal.strip()
    return [
        pattern
        for pattern in patterns
        if stripped == pattern or pattern in literal
    ]


def collect_references(repo_root: Path, policy: Policy, ownership: Ownership) -> tuple[list[Reference], list[str], int]:
    references: list[Reference] = []
    scanned: list[str] = []
    unparsed = 0
    all_roots = sorted(
        {root for subsystem in policy.subsystems for root in subsystem.roots if root not in {"", "."}},
        key=len,
        reverse=True,
    )
    inline_subsystems = [subsystem for subsystem in policy.subsystems if subsystem.inline_patterns]

    for path in iter_source_files(repo_root, policy):
        relpath = _relative(path, repo_root)
        if relpath in policy.fixture_paths:
            # This guard's own tests must name cross-boundary paths as string
            # fixtures. Exempting them is not a general escape hatch: the list
            # is a handful of tracked test files and is asserted to stay small.
            continue
        source = ownership.subsystem_for_path(relpath)
        if source is None:
            continue
        scanned.append(relpath)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relpath)
        except (SyntaxError, UnicodeDecodeError):
            # Deliberately tolerant: a file this checker cannot parse is
            # reported in the payload rather than crashing the run.
            unparsed += 1
            continue

        for line, dotted, symbol in _imported_names(tree):
            target = ownership.subsystem_for_module(dotted)
            if target is None or target.id == source.id:
                continue
            references.append(
                Reference(
                    path=relpath,
                    line=line,
                    kind="import",
                    symbol=symbol,
                    source=source.id,
                    target=target.id,
                    detail=dotted,
                )
            )

        for line, literal in _string_constants(tree):
            for subsystem in inline_subsystems:
                if subsystem.id == source.id:
                    continue
                for pattern in _inline_hits(literal, subsystem.inline_patterns):
                    references.append(
                        Reference(
                            path=relpath,
                            line=line,
                            kind="path-literal",
                            symbol=pattern,
                            source=source.id,
                            target=subsystem.id,
                            detail=literal[:200],
                        )
                    )
                    break
            for token in _literal_path_references(literal, all_roots):
                target = ownership.subsystem_for_path(token)
                if target is None:
                    continue
                references.append(
                    Reference(
                        path=relpath,
                        line=line,
                        kind="path-literal",
                        symbol=token,
                        source=source.id,
                        target=target.id,
                        detail=literal[:200],
                    )
                )
    return references, scanned, unparsed


# ---------------------------------------------------------------------------
# rule evaluation
# ---------------------------------------------------------------------------


def _skill_of(relpath: str) -> str | None:
    parts = relpath.split("/")
    if len(parts) >= 3 and parts[0] == ".agents" and parts[1] == "skills":
        return parts[2]
    return None


def _skill_publishes(repo_root: Path, skill: str, referenced: str) -> bool:
    """A sibling skill's script is published when its own SKILL.md names it."""
    skill_md = repo_root / ".agents" / "skills" / skill / "SKILL.md"
    if not skill_md.is_file():
        return False
    return referenced in skill_md.read_text(encoding="utf-8")


def evaluate(
    repo_root: Path,
    policy: Policy,
    ownership: Ownership,
    references: Iterable[Reference],
) -> list[Violation]:
    merged: dict[str, Violation] = {}

    def record(rule: Rule, reference: Reference, message: str) -> None:
        violation = Violation(
            rule=rule.id,
            severity=rule.severity,
            path=reference.path,
            symbol=reference.symbol,
            kind=reference.kind,
            source_subsystem=reference.source,
            target_subsystem=reference.target,
            message=message,
        )
        existing = merged.get(violation.fingerprint)
        if existing is None:
            violation.lines = [reference.line]
            merged[violation.fingerprint] = violation
        elif reference.line not in existing.lines:
            existing.lines.append(reference.line)

    for reference in references:
        source = policy.subsystem(reference.source)
        target = policy.subsystem(reference.target)

        # R4: an extracted subsystem referenced as in-repo content.
        if reference.symbol in target.inline_patterns:
            rule = policy.rule("extracted-inline-reference")
            record(
                rule,
                reference,
                f"names {target.repo} as in-repo content ({reference.symbol!r}); "
                f"after extraction it is a separate repository, not a branch of this one",
            )
            continue

        # R1: direction. A lower layer may never reference a higher one.
        if target.layer > source.layer:
            rule = policy.rule("layer-direction")
            record(
                rule,
                reference,
                f"{source.repo} ({source.id}, layer {source.layer}) references "
                f"{target.repo} ({target.id}, layer {target.layer}) via {reference.kind} "
                f"{reference.symbol!r}; this reverse dependency must be inverted, not carried over",
            )
            continue

        # R2: downward references must land on a published entry point.
        if target.id != source.id:
            published = (
                is_published_module(target, reference.detail)
                if reference.kind == "import"
                else is_published_path(target, reference.symbol)
            )
            if not published:
                rule = policy.rule("published-entry-point")
                record(
                    rule,
                    reference,
                    f"{source.id} reaches into {target.id} internals via {reference.kind} "
                    f"{reference.symbol!r}; {target.repo} publishes "
                    f"{', '.join(target.public_modules or target.public_paths) or 'nothing'}",
                )
                continue

        # R3: skill isolation, including inside the domain layer.
        if reference.kind == "path-literal":
            owning_skill = _skill_of(reference.symbol)
            referring_skill = _skill_of(reference.path)
            if (
                owning_skill
                and owning_skill != referring_skill
                and "/scripts/" in reference.symbol + "/"
                and not _skill_publishes(repo_root, owning_skill, reference.symbol)
            ):
                rule = policy.rule("skill-entry-point")
                record(
                    rule,
                    reference,
                    f"reaches into skill {owning_skill!r} internals ({reference.symbol}); "
                    f"only paths named in .agents/skills/{owning_skill}/SKILL.md are published",
                )
                continue

    return sorted(merged.values(), key=lambda item: (item.path, item.rule, item.symbol))


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    path: str
    generated_on: str
    note: str
    entries: dict[str, dict]


def load_baseline(path: Path, repo_root: Path) -> Baseline:
    raw = _read_json(path, "boundary baseline")
    if raw.get("version") != 1:
        raise BoundaryError(f"unsupported boundary baseline version: {raw.get('version')!r}")
    entries: dict[str, dict] = {}
    for entry in raw.get("accepted") or []:
        if not isinstance(entry, dict):
            raise BoundaryError("each accepted entry must be an object")
        for required in ("rule", "path", "symbol", "removed_by", "accepted_on"):
            if required not in entry:
                raise BoundaryError(f"accepted entry is missing {required!r}: {entry!r}")
        fingerprint = f"{entry['rule']}|{entry['path']}|{entry['symbol']}"
        if fingerprint in entries:
            raise BoundaryError(f"duplicate baseline entry: {fingerprint}")
        entries[fingerprint] = entry
    return Baseline(
        path=_relative(path, repo_root),
        generated_on=str(raw.get("generated_on", "")),
        note=str(raw.get("note", "")),
        entries=entries,
    )


def apply_baseline(violations: list[Violation], baseline: Baseline) -> tuple[list[Violation], list[dict], list[Violation]]:
    """Split violations into new vs accepted and report stale baseline rows."""
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
    return new, sorted(stale, key=lambda item: (item["path"], item["rule"], item["symbol"])), unattributed


def render_baseline(violations: list[Violation], baseline: Baseline, today: str) -> dict:
    """Rebuild the baseline, preserving hand-written attribution.

    New rows land as `removed_by: "unassigned"`, which `--mode enforce`
    rejects: regenerating the baseline cannot be a way to make the guard pass
    without deciding which extraction removes each violation.
    """
    accepted = []
    for violation in violations:
        previous = baseline.entries.get(violation.fingerprint, {})
        accepted.append(
            {
                "rule": violation.rule,
                "path": violation.path,
                "symbol": violation.symbol,
                "kind": violation.kind,
                "from": violation.source_subsystem,
                "to": violation.target_subsystem,
                "removed_by": str(previous.get("removed_by", UNATTRIBUTED)),
                "accepted_on": str(previous.get("accepted_on", today)),
                "why": str(previous.get("why", "")),
            }
        )
    accepted.sort(key=lambda item: (item["path"], item["rule"], item["symbol"]))
    return {
        "version": 1,
        "generated_on": baseline.generated_on or today,
        "note": baseline.note,
        "accepted": accepted,
        "accepted_counts_by_extraction": {
            key: sum(1 for item in accepted if item["removed_by"] == key)
            for key in sorted({item["removed_by"] for item in accepted})
        },
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


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
        f"policy {policy.path}: {len(policy.subsystems)} subsystems, {len(policy.rules)} rules, "
        f"skipping {len(policy.skip_roots)} roots"
    )
    ownership = Ownership(policy, repo_root)
    progress(f"resolved {ownership.module_count} importable module names to owning subsystems")

    references, scanned, unparsed = collect_references(repo_root, policy, ownership)
    progress(f"scanned {len(scanned)} source files; {len(references)} boundary-crossing references")
    if unparsed:
        progress(f"warning: {unparsed} file(s) could not be parsed and were skipped")

    violations = evaluate(repo_root, policy, ownership, references)
    baseline = load_baseline(baseline_path, repo_root)
    new, stale, unattributed = apply_baseline(violations, baseline)
    progress(
        f"{len(violations)} violation(s): {len(violations) - len(new)} accepted by "
        f"{baseline.path} (dated {baseline.generated_on or 'unknown'}), {len(new)} new"
    )
    for violation in new[:PROGRESS_DETAIL_LIMIT]:
        progress(f"  new {violation.rule} {violation.path}:{min(violation.lines)} {violation.symbol}")
    if len(new) > PROGRESS_DETAIL_LIMIT:
        progress(f"  ... and {len(new) - PROGRESS_DETAIL_LIMIT} more (see stdout payload)")
    for entry in stale[:PROGRESS_DETAIL_LIMIT]:
        progress(f"  stale baseline {entry['rule']} {entry['path']} {entry['symbol']}")
    if len(stale) > PROGRESS_DETAIL_LIMIT:
        progress(f"  ... and {len(stale) - PROGRESS_DETAIL_LIMIT} more stale entries")
    for violation in unattributed[:PROGRESS_DETAIL_LIMIT]:
        progress(f"  unattributed baseline entry {violation.rule} {violation.path} {violation.symbol}")

    by_rule: dict[str, int] = {}
    by_pair: dict[str, int] = {}
    for violation in violations:
        by_rule[violation.rule] = by_rule.get(violation.rule, 0) + 1
        pair = f"{violation.source_subsystem}->{violation.target_subsystem}"
        by_pair[pair] = by_pair.get(pair, 0) + 1
    by_extraction: dict[str, int] = {}
    for violation in violations:
        if violation.accepted:
            key = violation.removed_by or UNATTRIBUTED
            by_extraction[key] = by_extraction.get(key, 0) + 1

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
            "unparsed_files": unparsed,
            "references": len(references),
            "modules_resolved": ownership.module_count,
            "skipped_roots": sorted(policy.skip_roots),
            "fixture_paths": sorted(policy.fixture_paths),
        },
        "counts": {
            "violations": len(violations),
            "accepted": len(violations) - len(new),
            "new": len(new),
            "by_rule": dict(sorted(by_rule.items())),
            "by_direction": dict(sorted(by_pair.items())),
            "accepted_by_extraction": dict(sorted(by_extraction.items())),
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
            "Record the new violation in .agents/policy/repo-boundaries-baseline.json with the "
            "extraction that removes it, or invert the dependency."
            if new
            else "Delete the stale baseline rows in the same commit that fixed them."
            if stale
            else "Attribute every baseline row to an extraction (removed_by)."
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
    parser.add_argument("--policy", type=Path, help=f"boundary policy; defaults to {DEFAULT_POLICY}")
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
            baseline_path.write_text(json.dumps(rendered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            progress(f"rewrote {payload['baseline']['path']} with {len(rendered['accepted'])} accepted violation(s)")
            payload["baseline"]["rewritten"] = True
    except BoundaryError as exc:
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
