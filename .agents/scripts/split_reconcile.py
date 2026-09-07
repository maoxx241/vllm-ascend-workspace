#!/usr/bin/env python3
"""Reconcile what the repository split *declared* against what *arrived*.

`vllm-ascend-workspace` is being split into several repositories. Each
extraction wrote a handoff document and commit messages saying what it removed
and where that thing now lives. Nothing compared those declarations against the
destinations, and items fell into the gap between two repositories: each side
believed the other had it.

The split ledger (`.agents/policy/split-ledger.json`) records every declared
move as data: what the item is, the source repository, the declared
destination, who declared it, and a *recorded* verification state that a named
observer entered after looking at a specific destination commit. This script
re-derives the *observed* state from destination checkouts it is given and
reports, per row, one of three verdicts:

* `arrived`    - every piece of declared evidence was found in the destination;
* `missing`    - at least one piece was looked for and is not there;
* `unverified` - the destination could not be inspected (no checkout supplied,
                 checkout does not belong to that repository, or the evidence
                 needs a tool that is unavailable). Never reported as arrived.

A recorded state that disagrees with a definite observation is a hard failure
in `--mode enforce`: when a destination lands a fix, the ledger row must be
updated in the same change that acknowledges it, so "declared" and "arrived"
stay two separately attributed facts. There is deliberately no flag that
rewrites the ledger from observations.

Destination checkouts are passed with `--destination NAME=PATH` (repeatable)
or `VAWS_SPLIT_DESTINATIONS="name=path<os.pathsep>name=path"`. The scaffold
itself is always reachable as the repository containing this script. No
network access is attempted; two destinations are private, and a public CI job
that cannot read them reports `unverified` for their rows.

Progress goes to stderr; a single machine-readable JSON payload goes to stdout.

Exit codes: 0 clean (or `--mode report`), 1 ledger disagrees with an
observation, 2 unusable ledger or invocation.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DEFAULT_LEDGER = ".agents/policy/split-ledger.json"
STATES = ("arrived", "missing", "unverified")
EVIDENCE_KINDS = ("path", "symbols", "sha256", "text", "commit", "reference")
VISIBILITIES = ("public", "private")
PROGRESS_DETAIL_LIMIT = 12
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ALWAYS_SKIP_DIRS = frozenset({".git", "__pycache__"})


class LedgerError(RuntimeError):
    """Raised for an unusable ledger or invocation."""


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# ledger model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Repository:
    id: str
    repo: str
    visibility: str
    default_branch: str
    checkout: str | None  # "." marks the repository this script lives in
    receipt_path: str | None
    handoff: str | None


@dataclass(frozen=True)
class Declaration:
    repo: str
    commit: str | None
    document: str | None
    section: str | None
    says: str


@dataclass(frozen=True)
class Evidence:
    kind: str
    path: str | None = None
    glob: str | None = None
    exclude: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    sha256: str | None = None
    contains: str | None = None
    commit: str | None = None
    pattern: str | None = None

    def describe(self) -> str:
        where = self.path or self.glob or self.commit or "?"
        if self.kind == "symbols":
            return f"symbols {list(self.names)} in {where}"
        if self.kind == "sha256":
            return f"sha256 {str(self.sha256)[:12]}... of {where}"
        if self.kind == "text":
            return f"text {self.contains!r} in {where}"
        if self.kind == "commit":
            return f"commit {where} reachable from HEAD"
        if self.kind == "reference":
            return f"pattern {self.pattern!r} in {where}"
        return f"path {where}"


@dataclass(frozen=True)
class Recorded:
    state: str
    observed_on: str
    observed_by: str
    observed_commit: str | None
    follow_up: str | None
    why: str | None


@dataclass(frozen=True)
class Item:
    id: str
    what: str
    kind: str
    source_repo: str
    source_path: str | None
    source_scaffold_path: str | None
    destination_repo: str
    destination_path: str | None
    evidence: tuple[Evidence, ...]
    declared_by: tuple[Declaration, ...]
    recorded: Recorded
    notes: str


@dataclass(frozen=True)
class Ledger:
    version: int
    path: str
    sha256: str
    generated_on: str
    note: str
    skip_roots: frozenset[str]
    repositories: dict[str, Repository]
    items: tuple[Item, ...]

    @property
    def root_repository(self) -> Repository:
        for repository in self.repositories.values():
            if repository.checkout == ".":
                return repository
        raise LedgerError("no repository is marked as this checkout (\"checkout\": \".\")")


def _read_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise LedgerError(f"{label} does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LedgerError(f"{label} must be a JSON object")
    return payload


def _text(payload: dict, key: str, *, where: str, required: bool = True) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        if required:
            raise LedgerError(f"{where}: {key!r} is required")
        return None
    if not isinstance(value, str):
        raise LedgerError(f"{where}: {key!r} must be a string")
    return value


def _strings(payload: dict, key: str, *, where: str) -> tuple[str, ...]:
    value = payload.get(key) or []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise LedgerError(f"{where}: {key!r} must be a list of non-empty strings")
    return tuple(value)


def _relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_evidence(raw: object, where: str) -> Evidence:
    if not isinstance(raw, dict):
        raise LedgerError(f"{where}: each evidence entry must be an object")
    kind = _text(raw, "kind", where=where)
    if kind not in EVIDENCE_KINDS:
        raise LedgerError(f"{where}: unknown evidence kind {kind!r}; expected one of {list(EVIDENCE_KINDS)}")
    evidence = Evidence(
        kind=kind,
        path=_text(raw, "path", where=where, required=False),
        glob=_text(raw, "glob", where=where, required=False),
        exclude=_strings(raw, "exclude", where=where),
        names=_strings(raw, "names", where=where),
        sha256=_text(raw, "sha256", where=where, required=False),
        contains=_text(raw, "contains", where=where, required=False),
        commit=_text(raw, "commit", where=where, required=False),
        pattern=_text(raw, "pattern", where=where, required=False),
    )
    if kind in {"path", "sha256", "text"} and not evidence.path:
        raise LedgerError(f"{where}: evidence kind {kind!r} needs 'path'")
    if kind in {"symbols", "reference"} and not (evidence.path or evidence.glob):
        raise LedgerError(f"{where}: evidence kind {kind!r} needs 'path' or 'glob'")
    if kind == "symbols" and not evidence.names:
        raise LedgerError(f"{where}: evidence kind 'symbols' needs a non-empty 'names' list")
    if kind == "sha256" and not re.fullmatch(r"[0-9a-f]{64}", evidence.sha256 or ""):
        raise LedgerError(f"{where}: evidence kind 'sha256' needs a lowercase 64-hex 'sha256'")
    if kind == "text" and not evidence.contains:
        raise LedgerError(f"{where}: evidence kind 'text' needs 'contains'")
    if kind == "commit" and not _COMMIT_RE.match(evidence.commit or ""):
        raise LedgerError(f"{where}: evidence kind 'commit' needs a 7-40 hex 'commit'")
    if kind == "reference":
        if not evidence.pattern:
            raise LedgerError(f"{where}: evidence kind 'reference' needs 'pattern'")
        try:
            re.compile(evidence.pattern)
        except re.error as exc:
            raise LedgerError(f"{where}: invalid 'pattern': {exc}") from exc
    for part in (evidence.path, evidence.glob):
        if part and (part.startswith("/") or ".." in Path(part).parts):
            raise LedgerError(f"{where}: evidence paths must be relative and stay inside the checkout: {part!r}")
    return evidence


def _parse_recorded(raw: object, where: str) -> Recorded:
    if not isinstance(raw, dict):
        raise LedgerError(f"{where}: 'recorded' must be an object")
    state = _text(raw, "state", where=where)
    if state not in STATES:
        raise LedgerError(f"{where}: recorded state {state!r} must be one of {list(STATES)}")
    observed_on = _text(raw, "observed_on", where=where)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_on or ""):
        raise LedgerError(f"{where}: 'observed_on' must be an ISO date (YYYY-MM-DD)")
    observed_by = _text(raw, "observed_by", where=where)
    observed_commit = _text(raw, "observed_commit", where=where, required=False)
    follow_up = _text(raw, "follow_up", where=where, required=False)
    why = _text(raw, "why", where=where, required=False)
    if state in {"arrived", "missing"}:
        # A definite state means somebody looked at a specific destination
        # commit. Without it the row is an opinion, not an observation.
        if not observed_commit or not _COMMIT_RE.match(observed_commit):
            raise LedgerError(f"{where}: recorded state {state!r} needs 'observed_commit' (7-40 hex) naming the destination commit that was inspected")
    if state == "missing" and not follow_up:
        raise LedgerError(f"{where}: a recorded 'missing' row must name its 'follow_up' (who or what closes the gap)")
    if state == "unverified" and not why:
        raise LedgerError(f"{where}: a recorded 'unverified' row must say 'why' the destination could not be inspected")
    return Recorded(
        state=str(state),
        observed_on=str(observed_on),
        observed_by=str(observed_by),
        observed_commit=observed_commit,
        follow_up=follow_up,
        why=why,
    )


def load_ledger(path: Path, repo_root: Path) -> Ledger:
    raw = _read_json(path, "split ledger")
    if raw.get("version") != 1:
        raise LedgerError(f"unsupported split ledger version: {raw.get('version')!r}")

    repositories: dict[str, Repository] = {}
    raw_repositories = raw.get("repositories")
    if not isinstance(raw_repositories, dict) or not raw_repositories:
        raise LedgerError("'repositories' must be a non-empty object keyed by repository id")
    for repo_id, entry in raw_repositories.items():
        where = f"repositories[{repo_id!r}]"
        if not isinstance(entry, dict):
            raise LedgerError(f"{where}: must be an object")
        slug = _text(entry, "repo", where=where)
        if not _SLUG_RE.match(slug or ""):
            raise LedgerError(f"{where}: 'repo' must be an owner/name slug, got {slug!r}")
        visibility = _text(entry, "visibility", where=where)
        if visibility not in VISIBILITIES:
            raise LedgerError(f"{where}: 'visibility' must be one of {list(VISIBILITIES)}")
        repositories[str(repo_id)] = Repository(
            id=str(repo_id),
            repo=str(slug),
            visibility=str(visibility),
            default_branch=_text(entry, "default_branch", where=where, required=False) or "main",
            checkout=_text(entry, "checkout", where=where, required=False),
            receipt_path=_text(entry, "receipt_path", where=where, required=False),
            handoff=_text(entry, "handoff", where=where, required=False),
        )
    if sum(1 for repository in repositories.values() if repository.checkout == ".") != 1:
        raise LedgerError("exactly one repository must be marked as this checkout (\"checkout\": \".\")")

    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise LedgerError("'items' must be a non-empty list")
    items: list[Item] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw_items):
        where = f"items[{index}]"
        if not isinstance(entry, dict):
            raise LedgerError(f"{where}: must be an object")
        item_id = _text(entry, "id", where=where)
        where = f"items[{item_id!r}]"
        if item_id in seen_ids:
            raise LedgerError(f"{where}: duplicate id")
        seen_ids.add(str(item_id))

        source = entry.get("source")
        if not isinstance(source, dict):
            raise LedgerError(f"{where}: 'source' must be an object")
        source_repo = _text(source, "repo", where=f"{where}.source")
        if source_repo not in repositories:
            raise LedgerError(f"{where}.source: unknown repository {source_repo!r}")

        destination = entry.get("destination")
        if not isinstance(destination, dict):
            raise LedgerError(f"{where}: 'destination' must be an object naming the repository that received the item")
        destination_repo = destination.get("repo")
        if not destination_repo or not isinstance(destination_repo, str):
            # This is the row the whole mechanism exists to reject: a move
            # nobody attributed to a destination cannot be verified and must
            # not be accepted as recorded.
            raise LedgerError(f"{where}.destination: no attributed destination repository; a declared move must name where it went")
        if destination_repo not in repositories:
            raise LedgerError(f"{where}.destination: unknown repository {destination_repo!r}; add it to 'repositories' first")
        raw_evidence = destination.get("evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise LedgerError(f"{where}.destination: 'evidence' must be a non-empty list; an arrival nobody can check is not a destination")
        evidence = tuple(_parse_evidence(raw_e, f"{where}.destination.evidence[{i}]") for i, raw_e in enumerate(raw_evidence))

        raw_declared = entry.get("declared_by")
        if not isinstance(raw_declared, list) or not raw_declared:
            raise LedgerError(f"{where}: 'declared_by' must be a non-empty list; an item nobody declared does not belong in the ledger")
        declared: list[Declaration] = []
        for i, raw_d in enumerate(raw_declared):
            dwhere = f"{where}.declared_by[{i}]"
            if not isinstance(raw_d, dict):
                raise LedgerError(f"{dwhere}: must be an object")
            drepo = _text(raw_d, "repo", where=dwhere)
            if drepo not in repositories:
                raise LedgerError(f"{dwhere}: unknown repository {drepo!r}")
            commit = _text(raw_d, "commit", where=dwhere, required=False)
            document = _text(raw_d, "document", where=dwhere, required=False)
            if commit is not None and not _COMMIT_RE.match(commit):
                raise LedgerError(f"{dwhere}: 'commit' must be 7-40 hex")
            if not commit and not document:
                raise LedgerError(f"{dwhere}: needs a 'commit' or a 'document' that made the declaration")
            declared.append(
                Declaration(
                    repo=str(drepo),
                    commit=commit,
                    document=document,
                    section=_text(raw_d, "section", where=dwhere, required=False),
                    says=_text(raw_d, "says", where=dwhere) or "",
                )
            )

        items.append(
            Item(
                id=str(item_id),
                what=str(_text(entry, "what", where=where)),
                kind=str(_text(entry, "kind", where=where)),
                source_repo=str(source_repo),
                source_path=_text(source, "path", where=f"{where}.source", required=False),
                source_scaffold_path=_text(source, "scaffold_path", where=f"{where}.source", required=False),
                destination_repo=str(destination_repo),
                destination_path=_text(destination, "path", where=f"{where}.destination", required=False),
                evidence=evidence,
                declared_by=tuple(declared),
                recorded=_parse_recorded(entry.get("recorded"), f"{where}.recorded"),
                notes=str(entry.get("notes") or ""),
            )
        )

    scan = raw.get("scan") or {}
    if not isinstance(scan, dict):
        raise LedgerError("'scan' must be an object")
    return Ledger(
        version=1,
        path=_relative(path, repo_root),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        generated_on=str(raw.get("generated_on") or ""),
        note=str(raw.get("note") or ""),
        skip_roots=frozenset(_strings(scan, "skip_roots", where="scan")),
        repositories=repositories,
        items=tuple(items),
    )


# ---------------------------------------------------------------------------
# destination checkouts
# ---------------------------------------------------------------------------


@dataclass
class Checkout:
    repo_id: str
    path: Path | None
    reachable: bool
    reason: str
    head_commit: str | None = None
    origin_url: str | None = None
    is_git: bool = False
    receipt: dict | None = None
    receipt_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "reason": self.reason,
            "path": str(self.path) if self.path else None,
            "head_commit": self.head_commit,
            "is_git": self.is_git,
            "receipt_loaded": self.receipt is not None,
            "receipt_error": self.receipt_error,
        }


def _git(path: Path, *args: str) -> str | None:
    if shutil.which("git") is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _origin_matches(origin_url: str, slug: str) -> bool:
    normalized = origin_url.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized.lower().endswith("/" + slug.lower()) or normalized.lower().endswith(":" + slug.lower())


def _load_receipt(checkout_path: Path, receipt_path: str | None) -> tuple[dict | None, str | None]:
    """A destination may publish a tracked receipt attesting arrivals.

    Its absence is not an error. Its presence never yields `arrived` on its
    own: it is cross-checked against the evidence, and a receipt for an item
    whose evidence is absent is reported as a contradiction.
    """
    if not receipt_path:
        return None, None
    candidate = checkout_path / receipt_path
    if not candidate.is_file():
        return None, None
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"receipt {receipt_path} unreadable: {exc}"
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("items"), dict):
        return None, f"receipt {receipt_path} must be {{\"version\": 1, \"items\": {{<ledger id>: {{...}}}}}}"
    return payload, None


def resolve_checkouts(ledger: Ledger, repo_root: Path, supplied: dict[str, Path]) -> dict[str, Checkout]:
    unknown = sorted(set(supplied) - set(ledger.repositories))
    if unknown:
        raise LedgerError(f"--destination names repositories the ledger does not declare: {unknown}")
    checkouts: dict[str, Checkout] = {}
    for repo_id, repository in ledger.repositories.items():
        if repository.checkout == ".":
            path = repo_root
        elif repo_id in supplied:
            path = supplied[repo_id]
        else:
            checkouts[repo_id] = Checkout(
                repo_id=repo_id,
                path=None,
                reachable=False,
                reason=f"no checkout supplied for {repository.repo} ({repository.visibility}); pass --destination {repo_id}=PATH",
            )
            continue
        if not path.is_dir():
            checkouts[repo_id] = Checkout(repo_id=repo_id, path=path, reachable=False, reason=f"checkout path does not exist: {path}")
            continue
        checkout = Checkout(repo_id=repo_id, path=path, reachable=True, reason="checkout inspected")
        head = _git(path, "rev-parse", "HEAD")
        if head:
            checkout.is_git = True
            checkout.head_commit = head
            checkout.origin_url = _git(path, "remote", "get-url", "origin")
            if repository.checkout != "." and checkout.origin_url and not _origin_matches(checkout.origin_url, repository.repo):
                # A checkout of the wrong repository must not be allowed to
                # produce `arrived` for this one.
                checkout.reachable = False
                checkout.reason = f"checkout origin does not belong to {repository.repo}; refusing to treat it as that destination"
                checkouts[repo_id] = checkout
                continue
        checkout.receipt, checkout.receipt_error = _load_receipt(path, repository.receipt_path)
        checkouts[repo_id] = checkout
    return checkouts


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


@dataclass
class EvidenceResult:
    evidence: str
    status: str  # present | absent | unverifiable
    detail: str

    def to_dict(self) -> dict:
        return {"evidence": self.evidence, "status": self.status, "detail": self.detail}


def _iter_files(root: Path, skip_roots: frozenset[str]) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        pruned = []
        for name in sorted(dirnames):
            rel = (rel_dir / name).as_posix() if rel_dir.parts else name
            if name in _ALWAYS_SKIP_DIRS or rel in skip_roots or name in skip_roots:
                continue
            pruned.append(name)
        dirnames[:] = pruned
        for filename in sorted(filenames):
            yield Path(dirpath) / filename


def _matching_files(root: Path, evidence: Evidence, skip_roots: frozenset[str]) -> list[Path]:
    if evidence.path:
        candidate = root / evidence.path
        return [candidate] if candidate.is_file() else []
    matches: list[Path] = []
    for file in _iter_files(root, skip_roots):
        rel = file.relative_to(root).as_posix()
        if not fnmatch.fnmatchcase(rel, str(evidence.glob)):
            continue
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in evidence.exclude):
            continue
        matches.append(file)
    return matches


def _python_names(file: Path) -> set[str] | None:
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def check_evidence(checkout: Checkout, evidence: Evidence, skip_roots: frozenset[str]) -> EvidenceResult:
    assert checkout.path is not None
    root = checkout.path
    label = evidence.describe()

    if evidence.kind == "path":
        exists = (root / str(evidence.path)).exists()
        return EvidenceResult(label, "present" if exists else "absent", "exists" if exists else "not found in checkout")

    if evidence.kind == "sha256":
        target = root / str(evidence.path)
        if not target.is_file():
            return EvidenceResult(label, "absent", "file not found in checkout")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest == evidence.sha256:
            return EvidenceResult(label, "present", "digest matches")
        return EvidenceResult(label, "absent", f"digest differs: {digest[:12]}...")

    if evidence.kind == "text":
        target = root / str(evidence.path)
        if not target.is_file():
            return EvidenceResult(label, "absent", "file not found in checkout")
        try:
            body = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return EvidenceResult(label, "unverifiable", f"unreadable: {exc}")
        found = str(evidence.contains) in body
        return EvidenceResult(label, "present" if found else "absent", "marker found" if found else "marker not found")

    if evidence.kind == "commit":
        if not checkout.is_git:
            return EvidenceResult(label, "unverifiable", "checkout is not a git repository or git is unavailable")
        if shutil.which("git") is None:
            return EvidenceResult(label, "unverifiable", "git is not installed")
        completed = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", str(evidence.commit), "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode == 0:
            return EvidenceResult(label, "present", f"ancestor of {checkout.head_commit}")
        if completed.returncode == 1:
            return EvidenceResult(label, "absent", f"not an ancestor of {checkout.head_commit}")
        stderr = completed.stderr.strip().splitlines()
        if any("Not a valid" in line or "bad revision" in line for line in stderr):
            return EvidenceResult(label, "absent", "commit unknown to this checkout")
        return EvidenceResult(label, "unverifiable", f"git exited {completed.returncode}")

    files = _matching_files(root, evidence, skip_roots)
    if evidence.kind == "symbols":
        if not files:
            return EvidenceResult(label, "absent", "no matching file in checkout")
        wanted = set(evidence.names)
        unparsed = 0
        best_missing: set[str] | None = None
        for file in files:
            if file.suffix != ".py":
                continue
            names = _python_names(file)
            if names is None:
                unparsed += 1
                continue
            missing = wanted - names
            if not missing:
                return EvidenceResult(label, "present", f"all names defined in {file.relative_to(root).as_posix()}")
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
        if best_missing is None:
            if unparsed:
                return EvidenceResult(label, "unverifiable", f"{unparsed} matching file(s) could not be parsed as Python")
            return EvidenceResult(label, "absent", "no matching Python file in checkout")
        return EvidenceResult(label, "absent", f"no single file defines all names; closest lacks {sorted(best_missing)}")

    if evidence.kind == "reference":
        if not files:
            return EvidenceResult(label, "absent", "no matching file in checkout")
        regex = re.compile(str(evidence.pattern))
        unreadable = 0
        for file in files:
            try:
                body = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                unreadable += 1
                continue
            if regex.search(body):
                return EvidenceResult(label, "present", f"matched in {file.relative_to(root).as_posix()}")
        if unreadable == len(files):
            return EvidenceResult(label, "unverifiable", "no matching file could be read")
        return EvidenceResult(label, "absent", f"no match in {len(files)} matching file(s)")

    raise LedgerError(f"unhandled evidence kind {evidence.kind!r}")


# ---------------------------------------------------------------------------
# observation and comparison
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    state: str
    reason: str
    head_commit: str | None
    evidence: list[EvidenceResult] = field(default_factory=list)
    attested: bool | None = None  # None: no receipt published by the destination
    receipt_entry: dict | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "head_commit": self.head_commit,
            "evidence": [result.to_dict() for result in self.evidence],
            "attested": self.attested,
            "receipt": self.receipt_entry,
        }


def observe(item: Item, checkout: Checkout, skip_roots: frozenset[str]) -> Observation:
    if not checkout.reachable:
        return Observation("unverified", checkout.reason, checkout.head_commit)
    results = [check_evidence(checkout, evidence, skip_roots) for evidence in item.evidence]
    statuses = {result.status for result in results}
    if "absent" in statuses:
        state, reason = "missing", "declared evidence not found in destination"
    elif statuses == {"present"}:
        state, reason = "arrived", "all declared evidence found in destination"
    else:
        state, reason = "unverified", "some evidence could not be checked with the tools available"
    observation = Observation(state, reason, checkout.head_commit, results)
    if checkout.receipt is not None:
        entry = checkout.receipt["items"].get(item.id)
        observation.attested = entry is not None
        observation.receipt_entry = entry if isinstance(entry, dict) else None
    return observation


@dataclass
class Drift:
    item: str
    kind: str  # arrived-not-recorded | regression | recorded-unverified-now-observed | receipt-contradiction
    recorded: str
    observed: str
    message: str

    def to_dict(self) -> dict:
        return {"item": self.item, "kind": self.kind, "recorded": self.recorded, "observed": self.observed, "message": self.message}


def compare(item: Item, observation: Observation) -> Drift | None:
    recorded = item.recorded.state
    observed = observation.state
    if observation.attested and observed == "missing":
        return Drift(item.id, "receipt-contradiction", recorded, observed, "the destination's receipt attests this item but its declared evidence is absent; one of them is wrong")
    if observed == "unverified" or observed == recorded:
        return None
    if observed == "arrived":
        return Drift(item.id, "arrived-not-recorded", recorded, observed, f"destination now has the item at {observation.head_commit}; set recorded.state to \"arrived\" with observed_commit/observed_on/observed_by in the same change")
    if recorded == "arrived":
        return Drift(item.id, "regression", recorded, observed, f"recorded as arrived at {item.recorded.observed_commit} but the evidence is absent at {observation.head_commit}")
    return Drift(item.id, "recorded-unverified-now-observed", recorded, observed, f"row was recorded as unverified; the destination was inspected and the item is {observed}; record that observation")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def parse_destinations(values: list[str], env_value: str | None) -> dict[str, Path]:
    supplied: dict[str, Path] = {}
    specs = list(values)
    if env_value:
        specs.extend(part for part in env_value.split(os.pathsep) if part)
    for spec in specs:
        if "=" not in spec:
            raise LedgerError(f"--destination expects NAME=PATH, got {spec!r}")
        name, _, path = spec.partition("=")
        if not name or not path:
            raise LedgerError(f"--destination expects NAME=PATH, got {spec!r}")
        supplied[name] = Path(path).expanduser()
    return supplied


def run(repo_root: Path, ledger_path: Path, supplied: dict[str, Path], *, mode: str) -> dict:
    ledger = load_ledger(ledger_path, repo_root)
    progress(f"ledger {ledger.path} (dated {ledger.generated_on or 'unknown'}): {len(ledger.items)} declared item(s) across {len(ledger.repositories)} repositories")
    checkouts = resolve_checkouts(ledger, repo_root, supplied)
    for repo_id, checkout in checkouts.items():
        repository = ledger.repositories[repo_id]
        if checkout.reachable:
            progress(f"  {repo_id} ({repository.visibility}): inspecting {checkout.head_commit or 'non-git checkout'}")
        else:
            progress(f"  {repo_id} ({repository.visibility}): unverified - {checkout.reason}")
        if checkout.receipt_error:
            progress(f"  {repo_id}: warning: {checkout.receipt_error}")

    rows: list[dict] = []
    drifts: list[Drift] = []
    by_verdict = {state: 0 for state in STATES}
    by_recorded = {state: 0 for state in STATES}
    by_destination: dict[str, dict[str, int]] = {}
    for item in ledger.items:
        observation = observe(item, checkouts[item.destination_repo], ledger.skip_roots)
        drift = compare(item, observation)
        if drift:
            drifts.append(drift)
        by_verdict[observation.state] += 1
        by_recorded[item.recorded.state] += 1
        bucket = by_destination.setdefault(item.destination_repo, {state: 0 for state in STATES})
        bucket[observation.state] += 1
        rows.append(
            {
                "id": item.id,
                "what": item.what,
                "kind": item.kind,
                "source": {"repo": item.source_repo, "path": item.source_path, "scaffold_path": item.source_scaffold_path},
                "destination": {"repo": item.destination_repo, "path": item.destination_path},
                "declared_by": [
                    {"repo": d.repo, "commit": d.commit, "document": d.document, "section": d.section, "says": d.says}
                    for d in item.declared_by
                ],
                "recorded": {
                    "state": item.recorded.state,
                    "observed_on": item.recorded.observed_on,
                    "observed_by": item.recorded.observed_by,
                    "observed_commit": item.recorded.observed_commit,
                    "follow_up": item.recorded.follow_up,
                    "why": item.recorded.why,
                },
                "observed": observation.to_dict(),
                "verdict": observation.state,
                "drift": drift.to_dict() if drift else None,
                "notes": item.notes,
            }
        )

    progress(
        f"verdicts: {by_verdict['arrived']} arrived, {by_verdict['missing']} missing, "
        f"{by_verdict['unverified']} unverified; {len(drifts)} row(s) disagree with the ledger"
    )
    for row in rows:
        if row["verdict"] == "missing":
            progress(f"  missing   {row['id']} -> {row['destination']['repo']}")
    for drift in drifts[:PROGRESS_DETAIL_LIMIT]:
        progress(f"  drift     {drift.item}: recorded {drift.recorded}, observed {drift.observed} ({drift.kind})")
    if len(drifts) > PROGRESS_DETAIL_LIMIT:
        progress(f"  ... and {len(drifts) - PROGRESS_DETAIL_LIMIT} more (see stdout payload)")

    failed = mode == "enforce" and bool(drifts)
    return {
        "status": "failed" if failed else ("reported" if mode == "report" else "passed"),
        "mode": mode,
        "repo_root": str(repo_root),
        "ledger": {
            "path": ledger.path,
            "version": ledger.version,
            "generated_on": ledger.generated_on,
            "sha256": ledger.sha256,
            "items": len(ledger.items),
        },
        "destinations": {
            repo_id: {
                "repo": ledger.repositories[repo_id].repo,
                "visibility": ledger.repositories[repo_id].visibility,
                **checkout.to_dict(),
            }
            for repo_id, checkout in checkouts.items()
        },
        "counts": {
            "by_verdict": by_verdict,
            "by_recorded": by_recorded,
            "by_destination": by_destination,
            "drift": len(drifts),
        },
        "items": rows,
        "drift": [drift.to_dict() for drift in drifts],
        "next": (
            "Update the disagreeing ledger rows in .agents/policy/split-ledger.json so the recorded state, "
            "observed_commit, observed_on and observed_by match what was actually inspected."
            if drifts
            else "Rows marked unverified were not inspected; run again with --destination for those repositories "
            "before treating their recorded state as confirmed."
            if by_verdict["unverified"]
            else "No action required."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="workspace root; defaults to the repository containing this script",
    )
    parser.add_argument("--ledger", type=Path, help=f"split ledger; defaults to {DEFAULT_LEDGER}")
    parser.add_argument(
        "--destination",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="local checkout of a destination repository declared in the ledger (repeatable; "
        "also read from VAWS_SPLIT_DESTINATIONS, os.pathsep-separated). Destinations without a checkout report unverified.",
    )
    parser.add_argument(
        "--mode",
        choices=("enforce", "report"),
        default="enforce",
        help="enforce (default): fail when a recorded state disagrees with a definite observation. "
        "report: print the current state and always exit 0",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    ledger_path = args.ledger or repo_root / DEFAULT_LEDGER
    try:
        supplied = parse_destinations(args.destination, os.environ.get("VAWS_SPLIT_DESTINATIONS"))
        payload = run(repo_root, ledger_path, supplied, mode=args.mode)
    except LedgerError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc), "repo_root": str(repo_root)}, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
