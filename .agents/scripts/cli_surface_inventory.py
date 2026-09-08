#!/usr/bin/env python3
"""Enumerate the scaffold's CLI entry points and emit the inventory as data.

The scaffold ships many Python programs with a ``__main__`` guard. This tool
measures that surface from AST only so the current census, support roles and
external-owner boundaries can be checked as data. It does not implement the
historical thirteen-command dispatcher, import inspected scripts, fetch
providers, access NPU hosts or ask every command for ``--help``.

Definition of an *entry point* (deliberately mechanical, so the number is
reproducible):

- a tracked ``*.py`` file outside the ``vllm/`` and ``vllm-ascend/``
  submodules and outside ``.git/`` and untracked local state;
- not a test (``tests/`` directory, ``test_*.py``, ``selftest_*.py``,
  ``conftest.py``) and not a package ``__init__.py``;
- that either has an ``if __name__ == "__main__":`` guard or is a
  ``__main__.py`` module.

For every entry point the tool records, via ``ast`` (no imports of the
inspected code are performed):

- the parser style: ``argparse`` inline, ``delegated`` to a library function,
  or ``bare`` / ``bare-argv``;
- verbs (``add_parser(...)`` names and ``choices`` of a positional action
  argument);
- option strings from ``add_argument`` calls, including helper functions such
  as ``add_target_args(parser)`` resolved inside the same module;
- who references the script (skill docs, routing documents, other scripts,
  hooks, MCP/coordinator code, generated mirrors, policy, source-map pins,
  tests);
- the curated overlay: responsibility (mechanics / judgment / mixed),
  current support role, current target, optional historical proposed target,
  and optional committed external owner, loaded from
  ``.agents/policy/cli-surface-inventory.json``.

Progress is bounded on ``stderr``; one JSON payload is printed on ``stdout``.
The exit code is ``0`` when every discovered entry is classified coherently
and ``1`` when the overlay has drifted.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

SUBMODULES = ("vllm", "vllm-ascend")
IGNORED_PREFIXES = (".git/", ".vaws-local/", ".remote-dev/state/")
TEST_FILE_RE = re.compile(r"^(test_.*|.*_test|selftest_.*|conftest)\.py$")
REFERENCE_SUFFIXES = (".md", ".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".mdc")
ACTION_DESTS = {"action", "command", "operation", "mode", "subcommand", "verb", "op"}
MAX_PROGRESS_LINES = 12

RESPONSIBILITIES = ("mechanics", "judgment", "mixed")
SUPPORT_ROLES = (
    "supported",
    "compatibility",
    "internal",
    "generated",
    "hook",
    "payload",
    "harness",
)
TARGET_KINDS = ("self", "local-entry", "external", "non-command")
NON_COMMAND_TARGETS = frozenset({"guidance", "payload", "hook", "server", "harness"})
CATALOG_RELATIVE = ".agents/policy/cli-surface-inventory.json"
CATALOG_PATH = Path(__file__).resolve().parents[1] / "policy" / "cli-surface-inventory.json"

CURRENT_TABLE_BEGIN = "<!-- current-cli-surface-table -->"
CURRENT_TABLE_END = "<!-- /current-cli-surface-table -->"
HISTORICAL_TABLE_BEGIN = "<!-- historical-cli-surface-table -->"
HISTORICAL_TABLE_END = "<!-- /historical-cli-surface-table -->"

# Dated original #85 snapshot. Not a current total, CI invariant, or target.
HISTORICAL_SNAPSHOT = {
    "label": "original #85 b6e8559bc6e76743ffd08a383072c8b041a30e12",
    "status": "historical",
    "entry_point_count": 132,
    "by_category": {"mechanics": 81, "judgment": 8, "mixed": 8, "redundant": 35},
    "agent_facing_then": 114,
    "proposed_agent_command_count": 13,
    "proposed_verb_count": 75,
}
HISTORICAL_PROPOSED_COMMANDS = (
    "vaws remote",
    "vaws machine",
    "vaws session",
    "vaws sync",
    "vaws serve",
    "vaws bench",
    "vaws profile",
    "vaws model",
    "vaws workspace",
    "vaws manifest",
    "vaws knowledge",
    "vaws lint",
    "vaws task",
)

# Root-supplied integration facts for this census. The commit that lands the
# three owned files is an output of the work, not an input to this table.
ACCEPTED_PUBLIC_MAIN = "84f7e865a4e698d244c1cbe6cab6a2c5cca21067"
ACCEPTED_PUBLIC_TREE = "d8d5b42bfb07a97895b46281a664c7bec57e746d"
ORIGINAL_PR85 = "b6e8559bc6e76743ffd08a383072c8b041a30e12"
MERGE_PREVIEW_TREE = "626e553438e5b24451c8735682b0c0ce6e76d89c"
SCAFFOLD_SOURCE = "vllm-ascend-workspace/vllm-ascend-workspace"


def _cls(
    responsibility: str,
    support_role: str,
    note: str,
    *,
    target: str | None = None,
    target_kind: str = "self",
    proposed_target: str = "",
    external_owner: str = "",
) -> dict[str, str]:
    rec = {
        "responsibility": responsibility,
        "support_role": support_role,
        "target_kind": target_kind,
        "note": note,
    }
    if target:
        rec["target"] = target
    if proposed_target:
        rec["proposed_target"] = proposed_target
    if external_owner:
        rec["external_owner"] = external_owner
    return rec


class CatalogError(RuntimeError):
    """Raised when the curated CLI surface catalog cannot be used."""


def load_catalog(path: Path | None = None) -> dict:
    """Load overlay and owner-selector metadata from the plain JSON catalog.

    Missing or malformed data fails closed. The catalog is metadata only: it is
    not executed, and provider source is not fetched.
    """
    catalog_path = CATALOG_PATH if path is None else path
    try:
        raw = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"CLI surface catalog is missing: {catalog_path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"CLI surface catalog is not valid JSON: {catalog_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"CLI surface catalog root must be an object: {catalog_path}")
    classification = data.get("classification")
    declarations = data.get("dep_declarations")
    if not isinstance(classification, dict) or not classification:
        raise CatalogError(f"CLI surface catalog classification is missing or empty: {catalog_path}")
    if not isinstance(declarations, list) or not declarations:
        raise CatalogError(f"CLI surface catalog dep_declarations is missing or empty: {catalog_path}")
    parsed: dict[str, dict[str, str]] = {}
    for key, meta in classification.items():
        if not isinstance(key, str) or not key:
            raise CatalogError(f"CLI surface catalog has a non-string classification path: {catalog_path}")
        if not isinstance(meta, dict) or not meta:
            raise CatalogError(f"CLI surface catalog entry {key!r} is missing overlay fields: {catalog_path}")
        parsed[key] = {str(field): str(value) for field, value in meta.items()}
    deps: list[tuple[str, str]] = []
    labels: dict[str, str] = {}
    for item in declarations:
        if not isinstance(item, dict):
            raise CatalogError(f"CLI surface catalog dep_declarations entries must be objects: {catalog_path}")
        ident = item.get("id")
        package = item.get("package")
        if not isinstance(ident, str) or not ident or not isinstance(package, str) or not package:
            raise CatalogError(f"CLI surface catalog dep_declarations requires id and package: {catalog_path}")
        deps.append((ident, package))
        declaration = item.get("declaration")
        if isinstance(declaration, str) and declaration.strip():
            labels[ident] = declaration.strip()
            labels[package] = declaration.strip()
    loaded = dict(data)
    loaded["classification"] = parsed
    loaded["dep_declarations"] = deps
    loaded["dep_declaration_labels"] = labels
    return loaded


_CATALOG = load_catalog()
DEP_DECLARATIONS = tuple(_CATALOG["dep_declarations"])
DEP_DECLARATION_LABELS = dict(_CATALOG["dep_declaration_labels"])
CLASSIFICATION = dict(_CATALOG["classification"])


@dataclass
class Reference:
    path: str
    kind: str
    lines: list[int] = field(default_factory=list)


@dataclass
class EntryPoint:
    path: str
    area: str
    skill: str | None
    parser_style: str
    delegate: str | None
    verbs: list[str]
    options: list[str]
    positionals: list[str]
    docstring: str
    references: list[Reference]
    reference_kinds: dict[str, int]
    basename_collision: bool
    classification: dict[str, str] | None


@dataclass
class Finding:
    code: str
    path: str
    message: str


def progress(message: str, *, counter: list[int]) -> None:
    if counter[0] < MAX_PROGRESS_LINES:
        print(f"[cli-surface] {message}", file=sys.stderr)
    counter[0] += 1


def tracked_files(repo_root: Path) -> list[str]:
    """Tracked plus untracked-but-not-ignored files, so a new script is
    counted before it is committed. Falls back to a filesystem walk when the
    directory is not a Git checkout (used by the unit tests)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
        ).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    except (subprocess.CalledProcessError, FileNotFoundError):
        names = [
            p.relative_to(repo_root).as_posix()
            for p in repo_root.rglob("*")
            if p.is_file()
        ]
    kept: set[str] = set()
    for name in names:
        top = name.split("/", 1)[0]
        if top in SUBMODULES or name.startswith(IGNORED_PREFIXES):
            continue
        if not (repo_root / name).is_file():
            continue
        kept.add(name)
    return sorted(kept)


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return "tests" in parts[:-1] or bool(TEST_FILE_RE.match(parts[-1]))


def has_main_guard(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Compare) and len(test.comparators) == 1:
            left, right = test.left, test.comparators[0]
            names = {
                getattr(left, "id", None),
                getattr(right, "value", None),
            }
            if "__name__" in names and "__main__" in names:
                return True
    return False


def area_of(rel: str) -> str:
    if rel.startswith(".agents/skills/"):
        return ".agents/skills"
    if rel.startswith(".agents/scripts/"):
        return ".agents/scripts"
    if rel.startswith(".agents/hooks/"):
        return ".agents/hooks"
    if rel.startswith(".agents/maturation/"):
        return ".agents/maturation"
    if rel.startswith(".agents/coordinator/"):
        return ".agents/coordinator"
    if rel.startswith(".agents/"):
        return ".agents"
    if rel.startswith(".remote-dev/"):
        return ".remote-dev"
    if rel.startswith(".trae/"):
        return ".trae"
    if rel.startswith(".claude/"):
        return ".claude"
    return "other"


def skill_of(rel: str) -> str | None:
    parts = rel.split("/")
    if len(parts) > 3 and parts[0] in {".agents", ".trae", ".claude"} and parts[1] == "skills":
        return parts[2]
    return None


def _str_constants(node: ast.AST) -> list[str]:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _call_attr(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _keyword(node: ast.Call, name: str) -> ast.AST | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


class ParserCollector:
    """Collect argparse verbs and option strings from a body of statements."""

    def __init__(self, module: ast.Module, *, tool_literal: str | None = None) -> None:
        self.module = module
        self.tool_literal = tool_literal
        self.functions = {
            n.name: n for n in module.body if isinstance(n, ast.FunctionDef)
        }
        self.verbs: list[str] = []
        self.options: list[str] = []
        self.positionals: list[str] = []
        self.parser_calls = 0
        self._seen: set[str] = set()
        self._loop_vars: dict[str, list[str]] = {}

    def collect_function(self, name: str, depth: int = 0) -> None:
        if name in self._seen or depth > 3:
            return
        fn = self.functions.get(name)
        if fn is None:
            return
        self._seen.add(name)
        self._walk(fn.body, depth)

    def collect_module(self) -> None:
        self._walk(self.module.body, 0)
        for fn in self.functions.values():
            self.collect_function(fn.name, 1)

    def _branch_applies(self, test: ast.AST) -> bool:
        if self.tool_literal is None:
            return True
        literals = _str_constants(test)
        if not literals:
            return True
        return self.tool_literal in literals

    def _walk(self, body: Iterable[ast.stmt], depth: int) -> None:
        for stmt in body:
            if isinstance(stmt, ast.If):
                if self._branch_applies(stmt.test):
                    self._walk(stmt.body, depth)
                if not self._branch_applies(stmt.test) or not _str_constants(stmt.test):
                    self._walk(stmt.orelse, depth)
                continue
            if isinstance(stmt, (ast.For, ast.While, ast.With, ast.Try)):
                if isinstance(stmt, ast.For) and isinstance(stmt.target, ast.Name):
                    if isinstance(stmt.iter, (ast.Tuple, ast.List)):
                        self._loop_vars[stmt.target.id] = _str_constants(stmt.iter)
                inner = list(getattr(stmt, "body", []))
                for attr in ("orelse", "finalbody"):
                    inner.extend(getattr(stmt, attr, []) or [])
                for handler in getattr(stmt, "handlers", []) or []:
                    inner.extend(handler.body)
                self._walk(inner, depth)
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for node in ast.walk(stmt):
                if not isinstance(node, ast.Call):
                    continue
                attr = _call_attr(node)
                if attr == "ArgumentParser":
                    self.parser_calls += 1
                elif attr == "add_parser" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        self.verbs.append(first.value)
                    elif isinstance(first, ast.Name) and first.id in self._loop_vars:
                        self.verbs.extend(self._loop_vars[first.id])
                elif attr == "add_argument":
                    self._add_argument(node)
                elif isinstance(node.func, ast.Name) and node.func.id in self.functions:
                    self.collect_function(node.func.id, depth + 1)

    def _add_argument(self, node: ast.Call) -> None:
        names = [
            a.value
            for a in node.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
        if not names:
            return
        flags = [n for n in names if n.startswith("-")]
        if flags:
            self.options.append(flags[0])
            return
        positional = names[0]
        self.positionals.append(positional)
        dest_node = _keyword(node, "dest")
        dest = dest_node.value if isinstance(dest_node, ast.Constant) else positional
        choices = _keyword(node, "choices")
        if choices is not None and str(dest) in ACTION_DESTS:
            for value in _str_constants(choices):
                self.verbs.append(value)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _import_map(tree: ast.Module) -> dict[str, tuple[str, str]]:
    mapping: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                mapping[alias.asname or alias.name] = (node.module, alias.name)
    return mapping


def _main_guard_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, ast.If) and has_main_guard(ast.Module(body=[node], type_ignores=[])):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    calls.append(inner)
    return calls


def _candidate_module_paths(repo_root: Path, entry: Path, module: str) -> list[Path]:
    name = module.split(".")[-1] + ".py"
    return [
        entry.parent / name,
        repo_root / ".agents" / "lib" / name,
        repo_root / ".remote-dev" / "tools" / name,
        repo_root / ".remote-dev" / module.replace(".", "/") / "__init__.py",
        repo_root / ".remote-dev" / (module.replace(".", "/") + ".py"),
        entry.parent / module.replace(".", "/") / "__init__.py",
    ]


def analyse_entry(repo_root: Path, rel: str) -> tuple[str, str | None, list[str], list[str], list[str], str]:
    path = repo_root / rel
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    summary = doc[0] if doc else ""

    collector = ParserCollector(tree)
    collector.collect_module()
    if collector.parser_calls:
        return (
            "argparse",
            None,
            _dedupe(collector.verbs),
            _dedupe(collector.options),
            _dedupe(collector.positionals),
            summary,
        )

    imports = _import_map(tree)
    for call in _main_guard_calls(tree):
        target = call.func
        name = target.id if isinstance(target, ast.Name) else None
        if name is None or name not in imports:
            continue
        module, attr = imports[name]
        tool_literal = None
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            tool_literal = call.args[0].value
        for candidate in _candidate_module_paths(repo_root, path, module):
            if not candidate.is_file():
                continue
            lib_tree = ast.parse(candidate.read_text(encoding="utf-8"), filename=str(candidate))
            lib = ParserCollector(lib_tree, tool_literal=tool_literal)
            lib.collect_function(attr)
            for fn_name in ("build_parser", "_build_parser", "make_parser", "add_target_args", "add_endpoint_args"):
                if fn_name in lib.functions:
                    lib.collect_function(fn_name)
            if lib.parser_calls or lib.options or lib.verbs:
                label = f"{module}.{attr}" + (f"({tool_literal!r})" if tool_literal else "")
                lib_doc = (ast.get_docstring(lib_tree) or "").strip().splitlines()
                return (
                    "delegated",
                    label,
                    _dedupe(lib.verbs),
                    _dedupe(lib.options),
                    _dedupe(lib.positionals),
                    summary or (lib_doc[0] if lib_doc else ""),
                )
    uses_argv = any(
        isinstance(n, ast.Attribute) and n.attr == "argv" for n in ast.walk(tree)
    )
    return ("bare-argv" if uses_argv else "bare", None, [], [], [], summary)


INVENTORY_ARTIFACTS = frozenset(
    {
        ".agents/scripts/cli_surface_inventory.py",
        "docs/cli-surface.md",
        ".agents/policy/cli-surface-inventory.json",
    }
)

ROUTING_DOCS = {
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "README.en.md",
    ".agents/README.md",
    ".remote-dev/README.md",
    ".agents/coordinator/README.md",
}


def reference_kind(rel: str) -> str:
    if rel in ROUTING_DOCS or rel.startswith((".cursor/rules/", ".trae/rules/")):
        return "routing"
    if rel in {"pyproject.toml", "uv.lock"}:
        return "source-map"
    if rel.startswith((".agents/policy/", ".agents/leak-guard/")):
        return "policy"
    if rel.startswith((".claude/skills/", ".trae/skills/")):
        return "mirror"
    if rel.startswith((".claude/", ".codex/", ".cursor/")):
        return "client-config"
    if is_test_path(rel):
        return "test"
    if "/hooks/" in rel:
        return "hook"
    if rel.startswith((".remote-dev/mcp/", ".agents/coordinator/")):
        return "mcp"
    if rel.startswith("docs/"):
        return "docs"
    if rel.startswith(".agents/skills/") and rel.endswith(".md"):
        return "skill-doc"
    if rel.startswith(".agents/skills/") and rel.endswith((".yaml", ".yml", ".json")):
        return "skill-doc"
    if rel.endswith((".py", ".sh")):
        return "script"
    return "other"


def _collision_owner(line: str, rel: str, siblings: list[str]) -> bool:
    """Decide whether a line mentioning a colliding basename points at ``rel``.

    Only the path token directly attached to the basename is consulted, so
    ``.agents/scripts/x.py`` and ``.remote-dev/tools/x.py`` on one line are
    each attributed once. A bare basename with no directory is ambiguous and
    is attributed to every sibling.
    """
    base = rel.rsplit("/", 1)[-1]
    mine = rel.rsplit("/", 1)[0]
    others = [sib.rsplit("/", 1)[0] for sib in siblings if sib != rel]
    decided = False
    for match in re.finditer(r"([\w./-]*)" + re.escape(base), line):
        prefix = match.group(1)
        if prefix.rstrip("/").endswith(mine) or mine in prefix:
            return True
        if any(other in prefix for other in others):
            decided = True
            continue
        return True
    return not decided


def collect_references(
    repo_root: Path, files: list[str], entries: list[str]
) -> dict[str, list[Reference]]:
    by_base: dict[str, list[str]] = {}
    for rel in entries:
        by_base.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    refs: dict[str, dict[str, Reference]] = {rel: {} for rel in entries}
    for rel in files:
        if not rel.endswith(REFERENCE_SUFFIXES) or rel in INVENTORY_ARTIFACTS:
            continue
        try:
            text = (repo_root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for base, owners in by_base.items():
                if base not in line:
                    continue
                for owner in owners:
                    if owner == rel:
                        continue
                    if len(owners) > 1 and not _collision_owner(line, owner, owners):
                        continue
                    ref = refs[owner].setdefault(rel, Reference(rel, reference_kind(rel)))
                    ref.lines.append(lineno)
    return {rel: sorted(items.values(), key=lambda r: r.path) for rel, items in refs.items()}


def load_external_owners(repo_root: Path) -> dict[str, dict]:
    """Read committed package owners from pyproject.toml + uv.lock. Never fetch."""
    owners: dict[str, dict] = {}
    pyproject = repo_root / "pyproject.toml"
    lock = repo_root / "uv.lock"
    if not pyproject.is_file():
        return owners
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
        import vaws_dependency as deps
    except Exception:
        return owners
    try:
        locked = deps.locked_packages(repo_root)
    except Exception:
        locked = {}
    repos = {
        "vaws-remote-dev": "vllm-ascend-workspace/remote-dev",
        "remote-dev": "vllm-ascend-workspace/remote-dev",
        "vaws-coordinator": "vllm-ascend-workspace/vaws-coordinator",
        "vaws-knowledge": "vllm-ascend-workspace/vaws-knowledge",
        "vaws-top": "vllm-ascend-workspace/vaws-top",
    }
    for ident, package in DEP_DECLARATIONS:
        row = locked.get(package) or locked.get(ident) or {}
        owners[ident] = {
            "declaration": DEP_DECLARATION_LABELS.get(ident)
            or DEP_DECLARATION_LABELS.get(package)
            or "pyproject.toml",
            "name": package,
            "repository": repos.get(ident) or repos.get(package),
            "commit": row.get("commit"),
            "root_env": None,
            "consumed_surface": None,
            "note": "uv.lock is the only pin" if lock.is_file() else "uv.lock is missing",
            "source_availability": "uninspected",
        }
    return owners


def inspect_context(repo_root: Path) -> dict:
    ctx = {
        "accepted_public_main": ACCEPTED_PUBLIC_MAIN,
        "accepted_public_tree": ACCEPTED_PUBLIC_TREE,
        "original_pr85": ORIGINAL_PR85,
        "merge_preview_tree": MERGE_PREVIEW_TREE,
        "scaffold_source": SCAFFOLD_SOURCE,
        "definition": (
            "tracked or untracked-unignored *.py outside submodules/tests "
            "with a __main__ guard or __main__.py"
        ),
        "note": (
            "Census is AST of inspected files plus pyproject.toml / uv.lock. "
            "The commit SHA that lands this overlay is an output, not an input."
        ),
    }
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        ctx["inspected_head"] = head
        ctx["inspected_tree"] = tree
        ctx["working_tree_dirty"] = bool(porcelain)
    except (subprocess.CalledProcessError, FileNotFoundError):
        ctx["inspected_head"] = None
        ctx["inspected_tree"] = None
        ctx["working_tree_dirty"] = None
    return ctx


def _resolved_overlay(rel: str, meta: dict[str, str]) -> dict[str, str]:
    resolved = dict(meta)
    resolved.setdefault("target_kind", "self")
    if not resolved.get("target"):
        if resolved["target_kind"] in {"self", "non-command"}:
            resolved["target"] = rel if resolved["target_kind"] == "self" else resolved.get("support_role", rel)
        else:
            resolved["target"] = rel
    return resolved


def _validate_overlay(
    rel: str,
    meta: dict[str, str],
    entries: set[str],
    owners: dict[str, dict],
) -> list[Finding]:
    findings: list[Finding] = []
    responsibility = meta.get("responsibility")
    if responsibility not in RESPONSIBILITIES:
        findings.append(Finding("bad-responsibility", rel, f"unknown responsibility {responsibility!r}"))
    support_role = meta.get("support_role")
    if support_role not in SUPPORT_ROLES:
        findings.append(Finding("bad-support-role", rel, f"unknown support role {support_role!r}"))
    kind = meta.get("target_kind", "self")
    if kind not in TARGET_KINDS:
        findings.append(Finding("bad-target-kind", rel, f"unknown target_kind {kind!r}"))
    target = meta.get("target") or rel
    if kind == "local-entry" and target not in entries:
        findings.append(
            Finding(
                "local-target-missing",
                rel,
                f"local target {target!r} is not a discovered entry point",
            )
        )
    owner = meta.get("external_owner")
    if kind == "external":
        if not owner:
            findings.append(Finding("bad-external-target", rel, "external target_kind requires external_owner"))
        elif owner not in owners:
            findings.append(
                Finding(
                    "unknown-external-owner",
                    rel,
                    f"external_owner {owner!r} is not a committed dependency declaration",
                )
            )
    elif owner and owner not in owners:
        findings.append(
            Finding(
                "unknown-external-owner",
                rel,
                f"external_owner {owner!r} is not a committed dependency declaration",
            )
        )
    proposed = meta.get("proposed_target")
    if proposed and not (proposed.startswith("vaws ") or proposed in NON_COMMAND_TARGETS):
        findings.append(Finding("bad-proposed-target", rel, f"proposed_target {proposed!r} is not a labelled historical option"))
    return findings


def extract_delimited_table(text: str, begin: str, end: str) -> list[str]:
    if begin not in text or end not in text:
        return []
    section = text.split(begin, 1)[1].split(end, 1)[0]
    return [line for line in section.splitlines() if line.startswith("| `")]


def build_inventory(repo_root: Path, *, quiet: bool = False) -> dict:
    counter = [0]
    files = tracked_files(repo_root)
    if not quiet:
        progress(f"scanning {len(files)} tracked files under {repo_root.name}", counter=counter)

    entries: list[str] = []
    argparse_importers = 0
    for rel in files:
        if not rel.endswith(".py") or is_test_path(rel) or rel.endswith("__init__.py"):
            continue
        if rel.endswith("__main__.py"):
            entries.append(rel)
            continue
        try:
            tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if any(
            (isinstance(n, ast.Import) and any(a.name == "argparse" for a in n.names))
            or (isinstance(n, ast.ImportFrom) and n.module == "argparse")
            for n in ast.walk(tree)
        ):
            argparse_importers += 1
        if has_main_guard(tree):
            entries.append(rel)
    if not quiet:
        progress(f"found {len(entries)} entry points", counter=counter)

    references = collect_references(repo_root, files, entries)
    if not quiet:
        progress("resolved cross-references", counter=counter)

    owners = load_external_owners(repo_root)
    entry_set = set(entries)
    basenames: dict[str, int] = {}
    for rel in entries:
        base = rel.rsplit("/", 1)[-1]
        basenames[base] = basenames.get(base, 0) + 1

    records: list[EntryPoint] = []
    findings: list[Finding] = []
    for rel in entries:
        style, delegate, verbs, options, positionals, summary = analyse_entry(repo_root, rel)
        refs = references.get(rel, [])
        kinds: dict[str, int] = {}
        for ref in refs:
            kinds[ref.kind] = kinds.get(ref.kind, 0) + 1
        raw = CLASSIFICATION.get(rel)
        classification = _resolved_overlay(rel, raw) if raw else None
        if classification is None:
            findings.append(Finding("unclassified", rel, "entry point has no classification overlay"))
        else:
            findings.extend(_validate_overlay(rel, classification, entry_set, owners))
        records.append(
            EntryPoint(
                path=rel,
                area=area_of(rel),
                skill=skill_of(rel),
                parser_style=style,
                delegate=delegate,
                verbs=verbs,
                options=options,
                positionals=positionals,
                docstring=summary,
                references=refs,
                reference_kinds=dict(sorted(kinds.items())),
                basename_collision=basenames[rel.rsplit("/", 1)[-1]] > 1,
                classification=classification,
            )
        )
    for rel in sorted(CLASSIFICATION):
        if rel not in entry_set:
            findings.append(Finding("stale-classification", rel, "classified path is no longer an entry point"))

    by_area: dict[str, int] = {}
    by_responsibility: dict[str, int] = {name: 0 for name in RESPONSIBILITIES}
    by_support_role: dict[str, int] = {name: 0 for name in SUPPORT_ROLES}
    by_style: dict[str, int] = {}
    by_skill: dict[str, int] = {}
    by_target_kind: dict[str, int] = {}
    current_groups: dict[str, list[str]] = {name: [] for name in SUPPORT_ROLES}
    proposed_groups: dict[str, list[str]] = {}
    for record in records:
        by_area[record.area] = by_area.get(record.area, 0) + 1
        by_style[record.parser_style] = by_style.get(record.parser_style, 0) + 1
        if record.skill:
            by_skill[record.skill] = by_skill.get(record.skill, 0) + 1
        if not record.classification:
            continue
        cls = record.classification
        responsibility = cls["responsibility"]
        support_role = cls["support_role"]
        if responsibility in by_responsibility:
            by_responsibility[responsibility] += 1
        if support_role in by_support_role:
            by_support_role[support_role] += 1
            current_groups[support_role].append(record.path)
        kind = cls.get("target_kind", "self")
        by_target_kind[kind] = by_target_kind.get(kind, 0) + 1
        proposed = cls.get("proposed_target")
        if proposed:
            proposed_groups.setdefault(proposed, []).append(record.path)

    unreferenced = [
        r.path
        for r in records
        if not any(ref.kind not in {"test", "mirror"} for ref in r.references)
    ]
    classified = len(records) - len([f for f in findings if f.code == "unclassified"])
    if not quiet:
        progress(f"classified {classified}/{len(records)}", counter=counter)

    return {
        "status": "passed" if not findings else "failed",
        "repo_root_name": repo_root.name,
        "measurement": inspect_context(repo_root),
        "historical_snapshot": dict(HISTORICAL_SNAPSHOT),
        "proposed_surface": {
            "status": "historical-unimplemented",
            "label": "original #85 thirteen-command design; not a current command list or CI invariant",
            "agent_command_count": HISTORICAL_SNAPSHOT["proposed_agent_command_count"],
            "agent_commands": list(HISTORICAL_PROPOSED_COMMANDS),
            "verb_count": HISTORICAL_SNAPSHOT["proposed_verb_count"],
            "from_overlay": {key: sorted(val) for key, val in sorted(proposed_groups.items())},
        },
        "external_owners": owners,
        "entry_point_count": len(records),
        "counts": {
            "argparse_importing_files": argparse_importers,
            "by_area": dict(sorted(by_area.items())),
            "by_parser_style": dict(sorted(by_style.items())),
            "by_responsibility": by_responsibility,
            "by_support_role": by_support_role,
            "by_target_kind": dict(sorted(by_target_kind.items())),
            "by_skill": dict(sorted(by_skill.items())),
            "skills_with_entry_points": len(by_skill),
            "supported": by_support_role["supported"],
        },
        "current_surface": {key: sorted(val) for key, val in current_groups.items()},
        "unreferenced_outside_tests": unreferenced,
        "entry_points": [asdict(r) for r in records],
        "finding_count": len(findings),
        "findings": [asdict(f) for f in findings],
    }


def render_markdown(payload: dict) -> str:
    lines = [
        "| Entry point | Style | Verbs | Options | Refs | Responsibility | Support role | Current target | Proposed |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for record in payload["entry_points"]:
        cls = record["classification"] or {}
        verbs = ", ".join(record["verbs"]) or "-"
        refs = ", ".join(f"{k}:{v}" for k, v in record["reference_kinds"].items()) or "-"
        lines.append(
            "| `{path}` | {style} | {verbs} | {n_opts} | {refs} | {resp} | {role} | {target} | {proposed} |".format(
                path=record["path"],
                style=record["parser_style"],
                verbs=verbs,
                n_opts=len(record["options"]),
                refs=refs,
                resp=cls.get("responsibility", "unclassified"),
                role=cls.get("support_role", "-"),
                target=cls.get("target", "-"),
                proposed=cls.get("proposed_target", "-"),
            )
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="workspace root; defaults to the repository containing this script",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "summary"),
        default="json",
        help="json is the machine-readable payload; markdown renders the inventory table; summary prints counts only",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress stderr progress")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_inventory(args.repo_root.resolve(), quiet=args.quiet)
    if args.format == "markdown":
        sys.stdout.write(render_markdown(payload))
    elif args.format == "summary":
        summary = {
            k: payload[k]
            for k in (
                "status",
                "entry_point_count",
                "counts",
                "measurement",
                "historical_snapshot",
                "proposed_surface",
                "external_owners",
                "finding_count",
                "findings",
            )
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
