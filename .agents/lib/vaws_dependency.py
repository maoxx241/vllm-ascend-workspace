"""Shared pin schema, locator, and bootstrap for external VAWS checkouts.

Four extracted repositories are consumed as pinned checkouts, not submodules
and not vendored copies. This module is the single loader and identity check
for those pins. Locator wrappers in ``vaws_remote_dev``, ``vaws_coordinator``,
``manage_monitor``, and ``knowledge_kit`` stay as thin public adapters.

Pin drift (``off_pin``) is never an execution gate: ``resolve()`` still
returns the path so a developer checkout can run. Status commands report the
mismatch and exit 1 unless ``VAWS_DEPS_ALLOW_OFF_PIN`` names the dep.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from vaws_local_state import shared_workspace_root, utc_now_iso
from vaws_validate import ValidationError

ROOT = Path(__file__).resolve().parents[2]
DEPS_DIR = ROOT / ".agents" / "deps"
SCHEMA_PATH = ROOT / ".agents" / "schemas" / "dependency-v1.schema.json"
LEDGER_NAME = "deps-degradation.log"
LEDGER_MAX_LINES = 200
ALLOW_OFF_PIN_ENV = "VAWS_DEPS_ALLOW_OFF_PIN"
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
STATES = ("missing", "not_git", "wrong_origin", "off_pin", "ready")
VAWS_TOP_NAME = "vaws" + "-top"
KNOWN_PIN_FILES = (
    "remote-dev.json",
    "coordinator.json",
    VAWS_TOP_NAME + ".json",
    "vaws-knowledge.json",
)


class DependencyPinError(ValidationError):
    """Raised when a tracked pin file fails the v1 schema."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class DependencyUnavailable(RuntimeError):
    """Raised when ``resolve(required=True)`` cannot return a usable checkout."""


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _path_join(parent: str, key: str) -> str:
    if parent == "$":
        return f"$.{key}" if not key.startswith("[") else f"${key}"
    if key.startswith("["):
        return f"{parent}{key}"
    return f"{parent}.{key}"


def _validate_against_schema(instance: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Subset JSON Schema validator (stdlib). Field paths go on the error."""
    if "const" in schema and instance != schema["const"]:
        raise DependencyPinError(
            f"{path}: expected const {schema['const']!r}, got {instance!r}",
            field=path,
        )
    expected_type = schema.get("type")
    type_ok = {
        "object": isinstance(instance, dict) and not isinstance(instance, bool),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "boolean": isinstance(instance, bool),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
    }
    if expected_type and not type_ok.get(expected_type, False):
        raise DependencyPinError(
            f"{path}: expected type {expected_type}, got {type(instance).__name__}",
            field=path,
        )
    if "enum" in schema and instance not in schema["enum"]:
        raise DependencyPinError(
            f"{path}: {instance!r} is not one of {schema['enum']}",
            field=path,
        )
    if isinstance(instance, str):
        pattern = schema.get("pattern")
        if pattern and not re.fullmatch(pattern, instance):
            raise DependencyPinError(
                f"{path}: {instance!r} does not match {pattern}",
                field=path,
            )
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(instance) < min_len:
            raise DependencyPinError(
                f"{path}: string shorter than minLength {min_len}",
                field=path,
            )
    if expected_type == "object" and isinstance(instance, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in instance:
                raise DependencyPinError(
                    f"{_path_join(path, key)}: required field is missing",
                    field=_path_join(path, key),
                )
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                _validate_against_schema(value, properties[key], _path_join(path, key))
            elif additional is False:
                raise DependencyPinError(
                    f"{_path_join(path, key)}: additional property is not allowed",
                    field=_path_join(path, key),
                )
            elif isinstance(additional, dict):
                _validate_against_schema(value, additional, _path_join(path, key))
    if expected_type == "array" and isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate_against_schema(item, item_schema, _path_join(path, f"[{index}]"))


def _pin_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for filename in KNOWN_PIN_FILES:
        path = DEPS_DIR / filename
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DependencyPinError(
                f"$.: {path} is not valid JSON: {exc}", field="$"
            ) from exc
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str) and name:
            index[name] = path
    return index


def load_pin_file(path: Path) -> dict[str, Any]:
    """Validate one pin file against dependency-v1 and return it."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DependencyPinError(f"$.: pin file is missing: {path}", field="$") from exc
    except json.JSONDecodeError as exc:
        raise DependencyPinError(f"$.: pin file is not valid JSON: {exc}", field="$") from exc
    if not isinstance(data, dict):
        raise DependencyPinError("$.: pin root must be an object", field="$")
    _validate_against_schema(data, _schema())
    return data


def load_pin(name: str) -> dict[str, Any]:
    """Return the validated pin for ``name``. Raises ``DependencyPinError``."""
    index = _pin_index()
    path = index.get(name)
    if path is None:
        raise DependencyPinError(
            f"$.name: unknown dependency {name!r}; known: {sorted(index)}",
            field="$.name",
        )
    pin = load_pin_file(path)
    if pin.get("name") != name:
        raise DependencyPinError(
            f"$.name: pin file {path.name} has name {pin.get('name')!r}, expected {name!r}",
            field="$.name",
        )
    return pin


def all_pins() -> dict[str, dict[str, Any]]:
    """Load every tracked pin, keyed by ``name``."""
    return {name: load_pin_file(path) for name, path in sorted(_pin_index().items())}


def _mapping(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def expand_default_checkout(
    template: str,
    *,
    repo_root: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Expand ``{shared_workspace_root}``, ``{home}``, and ``{repo_dirname}``."""
    mapping = _mapping(env)
    home = mapping.get("HOME", "").strip()
    home_path = Path(home).expanduser() if home else Path.home()
    values = {
        "shared_workspace_root": str(shared_workspace_root(repo_root)),
        "home": str(home_path),
        "repo_dirname": repo_root.name,
    }
    unknown = set(PLACEHOLDER_RE.findall(template)) - set(values)
    if unknown:
        raise DependencyPinError(
            f"$.default_checkout: unknown placeholders {sorted(unknown)}",
            field="$.default_checkout",
        )
    return Path(template.format(**values)).expanduser()


def checkout_path(
    name: str,
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path = ROOT,
) -> tuple[Path, str]:
    """Resolved checkout path and its source (``env`` or ``default``)."""
    pin = load_pin(name)
    mapping = _mapping(env)
    configured = str(mapping.get(pin["root_env"], "")).strip()
    if configured:
        return Path(configured).expanduser(), "env"
    return expand_default_checkout(pin["default_checkout"], repo_root=repo_root, env=mapping), "default"


def _git(
    *argv: str,
    cwd: Path | None = None,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def git_head(path: Path) -> str | None:
    try:
        result = _git("-C", str(path), "rev-parse", "HEAD", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None


def git_origin(path: Path) -> str:
    try:
        result = _git("-C", str(path), "remote", "get-url", "origin", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_working_tree_clean(path: Path) -> bool:
    try:
        result = _git("-C", str(path), "status", "--porcelain", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and not result.stdout.strip()


def _normalize_origin(url: str) -> str:
    value = url.strip()
    ssh_host = "github.com"
    if value.startswith("git@") and value.split("@", 1)[-1].startswith(ssh_host + ":"):
        path = value.split(":", 1)[1]
        return f"https://github.com/{path.removesuffix('.git')}".rstrip("/").lower()
    parsed = value
    for scheme in ("ssh://", "git+ssh://"):
        prefix = scheme + "git@" + ssh_host + "/"
        if parsed.startswith(prefix):
            parsed = "https://github.com/" + parsed[len(prefix) :]
            break
    else:
        git_prefix = "git://" + ssh_host + "/"
        if parsed.startswith(git_prefix):
            parsed = "https://github.com/" + parsed[len(git_prefix) :]
    parsed = parsed.removesuffix(".git").rstrip("/")
    return parsed.lower()


def origins_match(actual: str, expected: str) -> bool:
    if not actual or not expected:
        return False
    return _normalize_origin(actual) == _normalize_origin(expected)


def _inspect_payload(
    *,
    name: str,
    path: Path,
    source: str,
    state: str,
    commit: str | None,
    pin_commit: str | None,
    pin_matches: bool | None,
    origin_matches: bool | None,
    problems: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "path": str(path),
        "source": source,
        "state": state,
        "commit": commit,
        "pin_commit": pin_commit,
        "pin_matches": pin_matches,
        "origin_matches": origin_matches,
        "problems": problems,
    }


def inspect(
    name: str,
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    """Describe one checkout. Never raises for missing or drifted trees."""
    try:
        pin = load_pin(name)
        path, source = checkout_path(name, env, repo_root=repo_root)
    except DependencyPinError as exc:
        return _inspect_payload(
            name=name,
            path=Path(""),
            source="default",
            state="missing",
            commit=None,
            pin_commit=None,
            pin_matches=None,
            origin_matches=None,
            problems=[str(exc)],
        )
    pin_commit = str(pin.get("commit") or "") or None
    identity = pin.get("identity") or {}
    required_files = list(identity.get("required_files") or [])
    canonical_origin = bool(identity.get("canonical_origin"))
    problems: list[str] = []
    if not path.exists():
        return _inspect_payload(
            name=name,
            path=path,
            source=source,
            state="missing",
            commit=None,
            pin_commit=pin_commit,
            pin_matches=None,
            origin_matches=None,
            problems=problems,
        )
    commit = git_head(path)
    if commit is None:
        problems.append("git rev-parse HEAD failed; not a git checkout")
        return _inspect_payload(
            name=name,
            path=path,
            source=source,
            state="not_git",
            commit=None,
            pin_commit=pin_commit,
            pin_matches=None,
            origin_matches=None,
            problems=problems,
        )
    origin = git_origin(path)
    origin_ok = origins_match(origin, str(pin.get("url") or ""))
    missing = [rel for rel in required_files if not (path / rel).is_file()]
    if missing:
        problems.append("missing required files: " + ", ".join(missing))
    if canonical_origin and not origin_ok:
        problems.append(
            f"origin {origin or '(none)'} does not match pin url {pin.get('url')}"
        )
        return _inspect_payload(
            name=name,
            path=path,
            source=source,
            state="wrong_origin",
            commit=commit,
            pin_commit=pin_commit,
            pin_matches=(commit == pin_commit) if pin_commit else None,
            origin_matches=False,
            problems=problems,
        )
    if missing:
        return _inspect_payload(
            name=name,
            path=path,
            source=source,
            state="not_git",
            commit=commit,
            pin_commit=pin_commit,
            pin_matches=(commit == pin_commit) if pin_commit else None,
            origin_matches=origin_ok if origin else None,
            problems=problems,
        )
    pin_matches = commit == pin_commit if pin_commit else None
    state = "ready" if pin_matches else "off_pin"
    if state == "off_pin":
        problems.append(f"HEAD {commit} does not match pin {pin_commit}")
    return _inspect_payload(
        name=name,
        path=path,
        source=source,
        state=state,
        commit=commit,
        pin_commit=pin_commit,
        pin_matches=pin_matches,
        origin_matches=origin_ok if origin else None,
        problems=problems,
    )


def resolve(
    name: str,
    *,
    required: bool = True,
    env: Mapping[str, str] | None = None,
    repo_root: Path = ROOT,
) -> Path | None:
    """Execution-path locator. ``off_pin`` is not a gate."""
    pin = load_pin(name)
    info = inspect(name, env, repo_root=repo_root)
    if info["state"] in {"ready", "off_pin"}:
        return Path(info["path"]).expanduser()
    if not required:
        return None
    raise DependencyUnavailable(
        f"{name} checkout is {info['state']} at {info['path']}; "
        f"clone it with `{pin['bootstrap']}` or set {pin['root_env']}"
    )


def allowed_off_pin_names(env: Mapping[str, str] | None = None) -> set[str]:
    mapping = _mapping(env)
    raw = str(mapping.get(ALLOW_OFF_PIN_ENV, "")).strip()
    if not raw:
        return set()
    if raw == "*":
        return {"*"}
    return {item.strip() for item in raw.split(",") if item.strip()}


def off_pin_allowed(name: str, env: Mapping[str, str] | None = None) -> bool:
    allowed = allowed_off_pin_names(env)
    return "*" in allowed or name in allowed


def status_exit_code(
    states: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> int:
    """Exit 1 unless every inspected dep is ready or an allowed off_pin."""
    for name, state in states.items():
        if state == "ready":
            continue
        if state == "off_pin" and off_pin_allowed(name, env):
            continue
        return 1
    return 0


def acknowledged_drift(env: Mapping[str, str] | None = None) -> list[str]:
    allowed = allowed_off_pin_names(env)
    if not allowed:
        return []
    if "*" in allowed:
        return ["*"]
    return sorted(allowed)


def _clone_url(url: str, dest: Path) -> subprocess.CompletedProcess[str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    return _git("clone", "--quiet", url, str(dest), timeout=120)


def _clone_gh(repository: str, dest: Path, ref: str) -> subprocess.CompletedProcess[str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["gh", "repo", "clone", repository, str(dest), "--", "--quiet", "--branch", ref],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def bootstrap(
    name: str,
    *,
    dest: Path | None = None,
    env: Mapping[str, str] | None = None,
    reset: bool = False,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    """Clone ``url`` at ``commit``. Never resets an off_pin tree unless asked."""
    pin = load_pin(name)
    mapping = _mapping(env)
    if dest is None:
        dest, source = checkout_path(name, mapping, repo_root=repo_root)
    else:
        dest = Path(dest).expanduser()
        source = "dest"
    dest = dest.expanduser()
    url = str(pin["url"])
    ref = str(pin["ref"])
    commit = str(pin["commit"])
    payload: dict[str, Any] = {
        "name": name,
        "dest": str(dest),
        "url": url,
        "ref": ref,
        "pinned_commit": commit,
        "source": source,
        "bootstrap": pin["bootstrap"],
        "root_env": pin["root_env"],
    }
    probe_env = dict(mapping)
    probe_env[pin["root_env"]] = str(dest)
    if dest.exists() and git_head(dest) is not None:
        info = inspect(name, probe_env, repo_root=repo_root)
        if info["state"] == "off_pin" and not reset:
            payload.update(
                state="off_pin",
                commit=info["commit"],
                pin_matches=False,
                remedy=(
                    f"checkout is at {info['commit']}, pin is {commit}; "
                    "pass --reset to fetch and checkout the pin when the working tree is clean"
                ),
            )
            return payload
        if info["state"] == "ready" and not reset:
            payload.update(state="ready", commit=info["commit"], pin_matches=True)
            return payload
        if info["state"] == "wrong_origin":
            payload.update(
                state="wrong_origin",
                commit=info["commit"],
                error=f"{dest} origin does not match {url}",
                remedy=f"choose a different dest or set {pin['root_env']}",
            )
            return payload
        if reset:
            if not git_working_tree_clean(dest):
                payload.update(
                    state="blocked",
                    commit=info["commit"],
                    error=f"{dest} has a dirty working tree; not resetting",
                    remedy="commit or stash local changes, then re-run with --reset",
                )
                return payload
            fetched = _git("fetch", "--quiet", "origin", ref, cwd=dest, timeout=120)
            if fetched.returncode != 0:
                payload.update(
                    state="failed",
                    error=(fetched.stderr or fetched.stdout).strip()[-2000:],
                )
                return payload
            checked = _git("checkout", "--quiet", "--detach", commit, cwd=dest)
            if checked.returncode != 0:
                payload.update(
                    state="failed",
                    error=(checked.stderr or checked.stdout).strip()[-2000:],
                    hint=f"pinned commit {commit} is not reachable from {ref}",
                )
                return payload
            head = git_head(dest)
            payload.update(state="ready", commit=head, pin_matches=head == commit)
            return payload
    if dest.exists():
        try:
            next(dest.iterdir())
        except StopIteration:
            pass
        else:
            if git_head(dest) is None:
                payload.update(
                    state="blocked",
                    error=f"{dest} exists and is not a git checkout",
                )
                return payload
    if not dest.exists() or (dest.is_dir() and not git_head(dest)):
        cloned = _clone_url(url, dest)
        if cloned.returncode != 0 and pin.get("visibility") == "private":
            if dest.exists():
                try:
                    dest.rmdir()
                except OSError:
                    pass
            if shutil.which("gh"):
                cloned = _clone_gh(str(pin["repository"]), dest, ref)
            if cloned.returncode != 0:
                payload.update(
                    state="failed",
                    error=(cloned.stderr or cloned.stdout).strip()[-2000:],
                    remedy="gh auth login",
                )
                return payload
        elif cloned.returncode != 0:
            if dest.exists():
                try:
                    dest.rmdir()
                except OSError:
                    pass
            payload.update(
                state="failed",
                error=(cloned.stderr or cloned.stdout).strip()[-2000:],
                remedy=f"clone {url} by hand and set {pin['root_env']}",
            )
            return payload
        checked = _git("checkout", "--quiet", "--detach", commit, cwd=dest)
        if checked.returncode != 0:
            fetched = _git("fetch", "--quiet", "origin", ref, cwd=dest, timeout=120)
            if fetched.returncode == 0:
                checked = _git("checkout", "--quiet", "--detach", commit, cwd=dest)
        if checked.returncode != 0:
            payload.update(
                state="failed",
                error=(checked.stderr or checked.stdout).strip()[-2000:],
                hint=f"pinned commit {commit} is not reachable from {ref}",
            )
            return payload
    info = inspect(name, probe_env, repo_root=repo_root)
    payload.update(
        state=info["state"],
        commit=info["commit"],
        pin_matches=info["pin_matches"],
        problems=info["problems"],
    )
    return payload


def degradation_log_path(repo_root: Path = ROOT) -> Path:
    return shared_workspace_root(repo_root) / ".vaws-local" / LEDGER_NAME


def record_hook_degradation(
    *,
    hook: str,
    dep: str,
    state: str,
    repo_root: Path = ROOT,
) -> Path:
    """Append one bounded ledger line. Creates ``.vaws-local`` as needed."""
    path = degradation_log_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{utc_now_iso()} hook={hook} dep={dep} state={state}"
    existing: list[str] = []
    if path.is_file():
        existing = path.read_text(encoding="utf-8").splitlines()
    existing.append(line)
    path.write_text("\n".join(existing[-LEDGER_MAX_LINES:]) + "\n", encoding="utf-8")
    return path


def capability_lost_for_dep(name: str) -> str:
    mapping = {
        "remote-dev": "remote_endpoints",
        "vaws-coordinator": "task_pool",
        VAWS_TOP_NAME: "fleet_observation",
        "vaws-knowledge": "conformance_kit",
    }
    return mapping.get(name, name)


def hook_skip_message(name: str, info: Mapping[str, Any]) -> str:
    pin = load_pin(name)
    capability = capability_lost_for_dep(name)
    return (
        f"{name} hook skipped: capability {capability} unavailable "
        f"(state={info.get('state')}). Bootstrap: {pin['bootstrap']}"
    )


def read_hook_degradations(*, limit: int = 10, repo_root: Path = ROOT) -> list[str]:
    path = degradation_log_path(repo_root)
    if not path.is_file():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return lines[-limit:]
