"""Report what pyproject requires, what uv.lock pins, and what is installed.

``uv.lock`` is the only pin. This module does not run git, does not clone, and
does not walk checkout trees. Status is one of ``missing``, ``off_spec``, or
``ready``.

``off_spec`` warns but does not block execution. ``missing`` makes capabilities
that depend on the package unavailable. The remedy for every package gap is
``uv sync``. CI uses ``uv lock --check`` to keep the lockfile aligned with
``pyproject.toml``.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

from vaws_validate import ValidationError

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = ROOT / "pyproject.toml"
LOCK_PATH = ROOT / "uv.lock"
REMEDY = "uv sync"
STATES = ("missing", "off_spec", "ready")
USABLE_STATES = frozenset({"ready", "off_spec"})
PACKAGE_NAMES = (
    "vaws-remote-dev",
    "vaws-coordinator",
    "vaws-knowledge",
)
VAWS_TOP_NAME = "vaws-top"
KNOWN_NAMES = PACKAGE_NAMES
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<spec>==\s*(?P<version>[^;]+))?"
)


class DependencyError(ValidationError):
    """Raised when pyproject.toml or uv.lock cannot be read for a package."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


# Kept so existing ``except DependencyPinError`` sites keep working during the
# cutover. New code should raise ``DependencyError``.
DependencyPinError = DependencyError


class DependencyUnavailable(RuntimeError):
    """Raised when a required package is missing from the current interpreter."""


def _norm_name(name: str) -> str:
    return name.replace("_", "-").lower()


def _parse_requirement(item: str) -> tuple[str, str | None]:
    raw = item.strip()
    if not raw or raw.startswith("#"):
        raise DependencyError(f"$.dependencies: empty requirement {item!r}", field="$.dependencies")
    match = REQUIREMENT_RE.match(raw)
    if match is None:
        raise DependencyError(
            f"$.dependencies: cannot parse requirement {item!r}",
            field="$.dependencies",
        )
    version = match.group("version")
    return _norm_name(match.group("name")), version.strip() if version else None


def load_pyproject(repo_root: Path = ROOT) -> dict[str, Any]:
    path = Path(repo_root) / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DependencyError(f"$.: pyproject.toml is missing: {path}", field="$") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DependencyError(f"$.: pyproject.toml is not valid TOML: {exc}", field="$") from exc
    if not isinstance(data, dict):
        raise DependencyError("$.: pyproject.toml root must be a table", field="$")
    return data


def required_versions(repo_root: Path = ROOT) -> dict[str, str | None]:
    """Return ``{normalized-name: exact-version-or-None}`` from ``[project]``."""
    project = load_pyproject(repo_root).get("project") or {}
    items = project.get("dependencies") or []
    if not isinstance(items, list):
        raise DependencyError("$.project.dependencies: expected an array", field="$.project.dependencies")
    versions: dict[str, str | None] = {}
    for item in items:
        if not isinstance(item, str):
            raise DependencyError(
                "$.project.dependencies: each entry must be a string",
                field="$.project.dependencies",
            )
        name, version = _parse_requirement(item)
        versions[name] = version
    return versions


def load_lock(repo_root: Path = ROOT) -> dict[str, Any]:
    path = Path(repo_root) / "uv.lock"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DependencyError(f"$.: uv.lock is missing: {path}", field="$") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DependencyError(f"$.: uv.lock is not valid TOML: {exc}", field="$") from exc
    if not isinstance(data, dict):
        raise DependencyError("$.: uv.lock root must be a table", field="$")
    return data


def _commit_from_git_source(source: Mapping[str, Any]) -> tuple[str | None, str | None]:
    git = source.get("git")
    if not isinstance(git, str) or not git:
        return None, None
    url, _, fragment = git.partition("#")
    commit = fragment.strip() or None
    if commit and not GIT_COMMIT_RE.fullmatch(commit):
        commit = unquote(commit)
        if not GIT_COMMIT_RE.fullmatch(commit):
            commit = None
    return url or None, commit


def locked_packages(repo_root: Path = ROOT) -> dict[str, dict[str, Any]]:
    """Return lock rows keyed by normalized package name."""
    packages = load_lock(repo_root).get("package") or []
    if not isinstance(packages, list):
        raise DependencyError("$.package: uv.lock package table is not an array", field="$.package")
    rows: dict[str, dict[str, Any]] = {}
    for item in packages:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        url, commit = _commit_from_git_source(source)
        rows[_norm_name(name)] = {
            "name": _norm_name(name),
            "version": item.get("version") if isinstance(item.get("version"), str) else None,
            "source": source,
            "url": url,
            "commit": commit,
        }
    return rows


def _direct_url(dist: metadata.Distribution) -> dict[str, Any] | None:
    try:
        raw = dist.read_text("direct_url.json")
    except (FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def installed_spec(name: str) -> dict[str, Any] | None:
    """Installed version and git commit from ``importlib.metadata``. No git."""
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None
    commit = None
    url = None
    requested = None
    direct = _direct_url(dist)
    if direct:
        url = direct.get("url") if isinstance(direct.get("url"), str) else None
        vcs = direct.get("vcs_info") if isinstance(direct.get("vcs_info"), dict) else {}
        commit_id = vcs.get("commit_id")
        if isinstance(commit_id, str) and GIT_COMMIT_RE.fullmatch(commit_id):
            commit = commit_id
        requested = vcs.get("requested_revision") if isinstance(vcs.get("requested_revision"), str) else None
    return {
        "name": _norm_name(name),
        "version": dist.version,
        "commit": commit,
        "url": url,
        "requested_revision": requested,
    }


def _unknown(name: str) -> None:
    raise DependencyError(
        f"$.name: unknown dependency {name!r}; known: {list(KNOWN_NAMES)}",
        field="$.name",
    )


def _payload(
    *,
    name: str,
    state: str,
    required_version: str | None,
    locked_version: str | None,
    locked_commit: str | None,
    locked_url: str | None,
    installed_version: str | None,
    installed_commit: str | None,
    problems: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "state": state,
        "required_version": required_version,
        "locked_version": locked_version,
        "locked_commit": locked_commit,
        "locked_url": locked_url,
        "installed_version": installed_version,
        "installed_commit": installed_commit,
        "problems": problems,
        "remedy": REMEDY,
    }


def inspect(name: str, repo_root: Path = ROOT) -> dict[str, Any]:
    """Describe one package. Never raises for missing or drifted installs."""
    key = _norm_name(name)
    if key not in PACKAGE_NAMES:
        return _payload(
            name=key,
            state="missing",
            required_version=None,
            locked_version=None,
            locked_commit=None,
            locked_url=None,
            installed_version=None,
            installed_commit=None,
            problems=[f"unknown dependency {name!r}"],
        )
    problems: list[str] = []
    try:
        required = required_versions(repo_root).get(key)
    except DependencyError as exc:
        return _payload(
            name=key,
            state="missing",
            required_version=None,
            locked_version=None,
            locked_commit=None,
            locked_url=None,
            installed_version=None,
            installed_commit=None,
            problems=[str(exc)],
        )
    try:
        locked = locked_packages(repo_root).get(key)
    except DependencyError as exc:
        locked = None
        problems.append(str(exc))
    locked_version = locked.get("version") if locked else None
    locked_commit = locked.get("commit") if locked else None
    locked_url = locked.get("url") if locked else None
    if locked is None and not problems:
        problems.append(f"{key} is not in uv.lock; run `{REMEDY}`")
    installed = installed_spec(key)
    if installed is None:
        problems.append(f"{key} is not installed in {sys.executable}; run `{REMEDY}`")
        return _payload(
            name=key,
            state="missing",
            required_version=required,
            locked_version=locked_version,
            locked_commit=locked_commit,
            locked_url=locked_url,
            installed_version=None,
            installed_commit=None,
            problems=problems,
        )
    installed_version = installed.get("version")
    installed_commit = installed.get("commit")
    if required and installed_version != required:
        problems.append(
            f"installed {key}=={installed_version} does not match pyproject {required}"
        )
    if locked_version and installed_version != locked_version:
        problems.append(
            f"installed {key}=={installed_version} does not match uv.lock {locked_version}"
        )
    if locked_commit and installed_commit and installed_commit != locked_commit:
        problems.append(
            f"installed commit {installed_commit} does not match uv.lock {locked_commit}"
        )
    elif locked_commit and not installed_commit:
        problems.append(f"installed {key} has no direct_url.json commit to compare with uv.lock")
    state = "ready" if not problems else "off_spec"
    return _payload(
        name=key,
        state=state,
        required_version=required,
        locked_version=locked_version,
        locked_commit=locked_commit,
        locked_url=locked_url,
        installed_version=installed_version,
        installed_commit=installed_commit,
        problems=problems,
    )


def all_packages(repo_root: Path = ROOT) -> dict[str, dict[str, Any]]:
    """Inspect every package this plane tracks, keyed by name."""
    return {name: inspect(name, repo_root=repo_root) for name in KNOWN_NAMES}


def status_exit_code(states: Mapping[str, str]) -> int:
    """Exit 1 unless every inspected dep is ``ready``."""
    for state in states.values():
        if state != "ready":
            return 1
    return 0


def require_package(name: str, repo_root: Path = ROOT) -> dict[str, Any]:
    info = inspect(name, repo_root=repo_root)
    if info["state"] == "missing":
        raise DependencyUnavailable(
            f"{name} is missing; install it with `{REMEDY}`"
        )
    return info


def package_usable(name: str, repo_root: Path = ROOT) -> bool:
    return inspect(name, repo_root=repo_root)["state"] in USABLE_STATES
