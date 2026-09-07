#!/usr/bin/env python3
"""Locate or bootstrap the standalone vaws-top checkout for local loopback serving.

The dashboard lives in `vllm-ascend-workspace/vaws-top`, not on a scaffold
branch. This consumer resolves an explicit checkout, records pin metadata, and
hands off to that repository's CLI/MCP/skill entrypoints. Only `ensure` may
clone or write consumer-owned `.env` keys. `status`, `restart`, `stop`, and
help never clone, fetch, checkout, install, or build.

vaws-top observations are not allocation authority. Coordinator execution
leases remain authoritative.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = REPO_ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import shared_inventory_path, shared_workspace_root  # noqa: E402

CANONICAL_HOST = "github.com"
CANONICAL_REPO = "vllm-ascend-workspace/vaws-top"
DEPENDENCY_FILE = REPO_ROOT / ".agents" / "deps" / (CANONICAL_REPO.rsplit("/", 1)[-1] + ".json")
DEFAULT_URL = "http://127.0.0.1:8789/api/health"
DASHBOARD_URL = "http://127.0.0.1:8788"
CLONE_ROOT_ENV = "VAWS_TOP_ROOT"
CONSUMER_ENV_KEYS = ("NFM_INVENTORY_FILES", "NFM_HOST_POOL_FILES", "NFM_BOOTSTRAP_COMMAND")
REQUIRED_FILES = (
    "package.json",
    "scripts/start.sh",
    "scripts/install-user-service.sh",
    "scripts/vaws-top.py",
    "scripts/vaws-top-mcp.py",
    "deploy/npu-fleet-monitor.service",
    ".agents/skills/vaws-top/SKILL.md",
)
AGENT_SKILL = ".agents/skills/vaws-top/SKILL.md"
CLI_ENTRY = "scripts/vaws-top.py"
MCP_ENTRY = "scripts/vaws-top-mcp.py"
START_ENTRY = "scripts/start.sh"
INSTALL_ENTRY = "scripts/install-user-service.sh"
MUTATING_GIT = {"clone", "fetch", "checkout", "reset", "worktree"}
_READ_ONLY_ACTIONS = False


class MonitorError(RuntimeError):
    pass


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    relay: bool = False,
) -> subprocess_result:
    if _READ_ONLY_ACTIONS:
        _assert_readonly_command(command)
    result = _subprocess_run(command, cwd=cwd)
    if relay:
        if result.stdout:
            print(result.stdout.rstrip(), file=sys.stderr)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()[-4000:]
        raise MonitorError(f"{' '.join(command)}: {detail}")
    return result


class subprocess_result:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _subprocess_run(command: list[str], *, cwd: Path | None = None) -> subprocess_result:
    import subprocess

    result = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return subprocess_result(result.returncode, result.stdout, result.stderr)


def default_clone_dir() -> Path:
    return Path.home() / "vaws-worktrees" / REPO_ROOT.name / "npu-fleet-monitor"


def resolve_clone_dir(requested: Path | None, env: Mapping[str, str] | None = None) -> tuple[Path, str]:
    if requested is not None:
        return requested.expanduser().resolve(), "cli"
    mapping = os.environ if env is None else env
    configured = str(mapping.get(CLONE_ROOT_ENV, "")).strip()
    if configured:
        return Path(configured).expanduser().resolve(), "env"
    return default_clone_dir().expanduser().resolve(), "default"


def load_pin(path: Path | None = None) -> dict[str, Any]:
    pin_path = path or DEPENDENCY_FILE
    if not pin_path.is_file():
        raise MonitorError(
            f"dependency pin is missing at {pin_path}; refusing to run an unpinned checkout"
        )
    try:
        data = json.loads(pin_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorError(f"dependency pin is not valid JSON: {pin_path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise MonitorError(f"unsupported dependency pin: {pin_path}")
    commit = str(data.get("commit") or "").strip()
    if not _looks_like_commit(commit):
        raise MonitorError(
            f"dependency pin {pin_path} has no exact commit; "
            "refusing to fall back to an unpinned ref"
        )
    repository = str(data.get("repository") or "").strip()
    if repository != CANONICAL_REPO:
        raise MonitorError(
            f"dependency pin repository identifier is {repository!r}, expected {CANONICAL_REPO}"
        )
    require_canonical_url(str(data.get("url") or "").strip(), what="dependency pin url")
    return data


def _looks_like_commit(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value.lower())


def _ambiguous_url_chars(value: str) -> bool:
    return any(char.isspace() or ord(char) < 32 for char in value)


def _exact_owner_name(path: str) -> str:
    parts = [item for item in path.strip("/").split("/") if item]
    if len(parts) != 2:
        return ""
    name = parts[1]
    if name.endswith(".git"):
        name = name[:-4]
    if not parts[0] or not name or "/" in name or name.endswith(".git"):
        return ""
    return f"{parts[0]}/{name}"


def github_repo_identity(value: str) -> str:
    """Return owner/name only for a clean GitHub HTTPS or SSH repository root."""
    if not value or _ambiguous_url_chars(value) or "?" in value or "#" in value:
        return ""
    if value.startswith("git@"):
        host, separator, path = value.partition(":")
        if not separator or "/" in host.split("@", 1)[-1]:
            return ""
        if host.rsplit("@", 1)[-1].lower() != CANONICAL_HOST:
            return ""
        return _exact_owner_name(path)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"https", "ssh", "git", "git+ssh"}:
        return ""
    if (parsed.hostname or "").lower() != CANONICAL_HOST:
        return ""
    if parsed.port is not None:
        return ""
    if parsed.scheme == "https" and (parsed.username or parsed.password):
        return ""
    if parsed.scheme in {"ssh", "git", "git+ssh"} and parsed.username not in {None, "git"}:
        return ""
    return _exact_owner_name(parsed.path)


def require_canonical_url(url: str, *, what: str) -> str:
    identity = github_repo_identity(url)
    if identity != CANONICAL_REPO:
        raise MonitorError(
            f"{what} is not a canonical GitHub repository transport root for "
            f"{CANONICAL_REPO} (host {CANONICAL_HOST}): {url}"
        )
    return url


def git_toplevel(path: Path) -> Path | None:
    result = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).expanduser().resolve()


def git_common_dir(path: Path) -> Path | None:
    result = run(["git", "-C", str(path), "rev-parse", "--git-common-dir"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    common = Path(result.stdout.strip()).expanduser()
    if not common.is_absolute():
        common = path / common
    return common.resolve()


def git_origin(path: Path) -> str:
    result = run(["git", "-C", str(path), "remote", "get-url", "origin"], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_head(path: Path) -> str:
    return run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()


def git_source_dirty(path: Path) -> str:
    return run(["git", "-C", str(path), "status", "--porcelain"]).stdout.strip()


def same_path(left: Path, right: Path) -> bool:
    left_resolved = left.expanduser().resolve()
    right_resolved = right.expanduser().resolve()
    if left_resolved == right_resolved:
        return True
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def directory_is_empty(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    return False


def reject_nested_path(dest: Path) -> None:
    probe = dest if dest.exists() else dest.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        return
    toplevel = git_toplevel(probe)
    if toplevel is None:
        return
    resolved = dest.resolve()
    if same_path(resolved, toplevel):
        return
    raise MonitorError(
        f"nested checkout path {resolved} is inside git root {toplevel}; "
        f"choose a Git root of {CANONICAL_REPO} or an absent/empty directory"
    )


def is_legacy_scaffold_checkout(path: Path, scaffold_root: Path | None = None) -> bool:
    scaffold_root = REPO_ROOT if scaffold_root is None else scaffold_root
    if not path.exists():
        return False
    clone_common = git_common_dir(path)
    scaffold_common = git_common_dir(scaffold_root)
    if clone_common is not None and scaffold_common is not None and clone_common == scaffold_common:
        return True
    origin = git_origin(path)
    scaffold_origin = git_origin(scaffold_root)
    if origin and scaffold_origin and github_repo_identity(origin) != CANONICAL_REPO:
        if git_toplevel(path) is not None and _same_remote(origin, scaffold_origin):
            return True
    return False


def _same_remote(left: str, right: str) -> bool:
    left_id = github_repo_identity(left)
    right_id = github_repo_identity(right)
    if left_id and right_id:
        return left_id == right_id
    return left.rstrip("/").removesuffix(".git") == right.rstrip("/").removesuffix(".git")


def looks_like_checkout(path: Path) -> bool:
    return all((path / relative).is_file() for relative in REQUIRED_FILES)


def inspect_existing(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        raise MonitorError(f"checkout path exists and is not a directory: {path}")
    toplevel = git_toplevel(path)
    if toplevel is None:
        if directory_is_empty(path):
            return
        raise MonitorError(
            f"non-Git directory is not empty and will not be modified: {path}"
        )
    if not same_path(toplevel, path):
        raise MonitorError(
            f"nested checkout path {path.resolve()} is not the git root {toplevel}; "
            "the existing tree was not modified"
        )
    origin = git_origin(path)
    if not origin:
        raise MonitorError(f"existing git root {path} has no origin remote; refusing to use it")
    require_canonical_url(origin, what=f"existing origin at {path}")


def locate_checkout(
    dest: Path,
    *,
    create: bool,
    pin: dict[str, Any],
    url: str,
) -> tuple[Path, bool]:
    dest = dest.expanduser().resolve()
    reject_nested_path(dest)
    if is_legacy_scaffold_checkout(dest):
        raise MonitorError(
            f"{dest} is a legacy scaffold monitor worktree sharing this repository's "
            "Git directory and may contain private runtime data. Choose a separate "
            f"destination with --clone-dir or {CLONE_ROOT_ENV}. This tool will not "
            "change its origin, reset it, delete it, detach it, move data, or import keys."
        )
    inspect_existing(dest)
    toplevel = git_toplevel(dest) if dest.exists() else None
    if dest.exists() and toplevel is not None and same_path(toplevel, dest):
        return dest, False
    if not create:
        raise MonitorError(
            f"no monitor checkout at {dest}; run "
            "`python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py ensure` "
            f"or set {CLONE_ROOT_ENV}"
        )
    if dest.exists() and not directory_is_empty(dest):
        raise MonitorError(f"target exists and is not an empty monitor checkout: {dest}")
    progress(f"Cloning {pin['repository']} at {pin['commit']} into {dest}")
    clone_repository(url, dest, str(pin["commit"]))
    return dest, True


def clone_repository(url: str, dest: Path, commit: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--quiet", url, str(dest)], relay=True)
    head = git_head(dest)
    if head != commit:
        run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", commit], relay=True)


def validate_existing_checkout(
    clone: Path,
    pin: dict[str, Any],
    *,
    require_clean_for_ensure: bool,
) -> str:
    missing = [name for name in REQUIRED_FILES if not (clone / name).is_file()]
    if missing:
        raise MonitorError(f"monitor checkout is missing required files: {', '.join(missing)}")
    origin = git_origin(clone)
    require_canonical_url(origin, what=f"origin of {clone}")
    commit = git_head(clone)
    pinned = str(pin["commit"])
    dirty = git_source_dirty(clone)
    if require_clean_for_ensure and dirty:
        raise MonitorError(
            "monitor checkout has source changes; they were preserved and not reset. "
            "Commit or move them before ensure"
        )
    if commit != pinned:
        raise MonitorError(
            f"monitor checkout {clone} is at {commit}, pinned commit is {pinned}. "
            "Refusing to fetch, checkout, reset, or silently advance a divergent tree"
        )
    return commit


def default_bootstrap_command(repo_root: Path | None = None) -> str:
    repo_root = REPO_ROOT if repo_root is None else repo_root
    script = repo_root / ".agents" / "skills" / "machine-management" / "scripts" / "manage_machine.py"
    return " ".join(
        [
            "{python}",
            shlex.quote(str(script)),
            "bootstrap-host-key",
            "--host",
            "{host}",
            "--host-port",
            "{port}",
            "--user",
            "{user}",
            "--public-key-file",
            "{public_key_file}",
            "--password-stdin",
        ]
    )


def render_bootstrap_argv(
    template: str,
    *,
    host: str,
    port: str,
    user: str,
    public_key_file: str,
    python: str,
) -> list[str]:
    values = {
        "host": host,
        "port": str(port),
        "user": user,
        "public_key_file": public_key_file,
        "python": python,
    }
    argv = shlex.split(template)
    if not argv:
        raise MonitorError("bootstrap command must not be empty")
    rendered: list[str] = []
    for argument in argv:
        try:
            rendered.append(argument.format(**values))
        except (KeyError, IndexError, ValueError) as exc:
            raise MonitorError(f"unsupported placeholder in bootstrap command: {argument}") from exc
    return rendered


def _reject_ambiguous_path(path: Path, *, label: str) -> Path:
    text = str(path)
    if "\n" in text or "\r" in text:
        raise MonitorError(f"{label} path contains a newline and will not be used as an inventory source")
    if os.pathsep in text:
        raise MonitorError(
            f"{label} path contains the path-list delimiter {os.pathsep!r}; "
            "pass each file as its own path instead of embedding extra sources"
        )
    return path


def default_inventory_files(repo_root: Path | None = None) -> list[Path]:
    repo_root = REPO_ROOT if repo_root is None else repo_root
    shared = _reject_ambiguous_path(shared_inventory_path(repo_root), label="shared inventory")
    files = [shared]
    seen = {shared.resolve()}
    primary = shared_workspace_root(repo_root)
    candidates = [
        repo_root.resolve() / ".machine-inventory.json",
        primary / ".machine-inventory.json",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        files.append(_reject_ambiguous_path(candidate, label="compatibility inventory"))
        seen.add(resolved)
    return files


def default_host_pool_files(repo_root: Path | None = None) -> list[Path]:
    repo_root = REPO_ROOT if repo_root is None else repo_root
    primary = shared_workspace_root(repo_root)
    files: list[Path] = []
    seen: set[Path] = set()
    for candidate in (repo_root.resolve() / "hosts.txt", primary / "hosts.txt"):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        files.append(_reject_ambiguous_path(candidate, label="host pool"))
        seen.add(resolved)
    return files


def parse_path_list(raw: str, *, label: str) -> list[Path]:
    if "\n" in raw or "\r" in raw:
        raise MonitorError(f"{label} contains a newline and will not be interpreted as extra variables or files")
    paths: list[Path] = []
    seen: set[Path] = set()
    for item in raw.split(os.pathsep):
        text = item.strip()
        if not text:
            continue
        path = _reject_ambiguous_path(Path(text).expanduser(), label=label)
        resolved = path if not path.exists() else path.resolve()
        if resolved in seen:
            continue
        paths.append(path)
        seen.add(resolved)
    return paths


def join_path_list(paths: list[Path]) -> str:
    return os.pathsep.join(str(path) for path in paths)


def parse_env_file(text: str) -> list[tuple[str | None, str | None, str]]:
    """Return (key, value, raw_line) rows. Comments/blanks have key None."""
    rows: list[tuple[str | None, str | None, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            rows.append((None, None, line))
            continue
        if "=" not in stripped:
            raise MonitorError("clone .env has a line that is not KEY=VALUE and will not be rewritten")
        key, _, remainder = stripped.partition("=")
        key = key.strip()
        if not key or any(char.isspace() for char in key):
            raise MonitorError(f"clone .env has an ambiguous key {key!r}")
        rows.append((key, _unescape_env_value(remainder), line))
    if text.splitlines() and text.endswith("\n") is False:
        pass
    return rows


_DOUBLE_QUOTE_ESCAPES = frozenset({"\\", '"', "$", "`"})


def _fail_env_syntax(message: str) -> None:
    raise MonitorError(message)


def _unescape_backslash_run(inner: str, *, specials: frozenset[str] | None) -> str:
    chars: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char != "\\":
            chars.append(char)
            index += 1
            continue
        if index + 1 >= len(inner):
            _fail_env_syntax("clone .env has a dangling backslash")
        nxt = inner[index + 1]
        if nxt in "\n\r":
            _fail_env_syntax("clone .env has an unsupported line continuation")
        if specials is None:
            chars.append(nxt)
        elif nxt in specials:
            chars.append(nxt)
        else:
            chars.append("\\")
            chars.append(nxt)
        index += 2
    return "".join(chars)


def _parse_double_quoted(text: str) -> tuple[str, str]:
    chars: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(chars), text[index + 1 :]
        if char != "\\":
            chars.append(char)
            index += 1
            continue
        if index + 1 >= len(text):
            _fail_env_syntax("clone .env has a dangling backslash")
        nxt = text[index + 1]
        if nxt in "\n\r":
            _fail_env_syntax("clone .env has an unsupported line continuation")
        if nxt in _DOUBLE_QUOTE_ESCAPES:
            chars.append(nxt)
        else:
            chars.append("\\")
            chars.append(nxt)
        index += 2
    _fail_env_syntax("clone .env has an unclosed double quote")
    raise AssertionError("unreachable")


def _parse_single_quoted(text: str) -> tuple[str, str]:
    index = 1
    while index < len(text):
        if text[index] == "'":
            return text[1:index], text[index + 1 :]
        index += 1
    _fail_env_syntax("clone .env has an unclosed single quote")
    raise AssertionError("unreachable")


def _unescape_env_value(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if text[0] == '"':
        value, rest = _parse_double_quoted(text)
        if rest:
            _fail_env_syntax("clone .env has extra content after a quoted value")
        return value
    if text[0] == "'":
        value, rest = _parse_single_quoted(text)
        if rest:
            _fail_env_syntax("clone .env has extra content after a quoted value")
        return value
    if '"' in text or "'" in text:
        _fail_env_syntax("clone .env has an unquoted value with an embedded quote")
    return _unescape_backslash_run(text, specials=None)


def encode_env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise MonitorError("refusing to write an NFM value containing a newline")
    if value and all(char not in value for char in ' \t#"\'\\$`') and "=" not in value:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def existing_env_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if "\0" in text:
        raise MonitorError("clone .env contains NUL and will not be parsed")
    values: dict[str, str] = {}
    for key, value, _raw in parse_env_file(text):
        if key is not None and value is not None:
            values[key] = value
    return values


def merge_consumer_env(
    existing: dict[str, str],
    desired: dict[str, str | None],
    *,
    explicit: set[str],
) -> dict[str, str | None]:
    merged: dict[str, str | None] = {}
    for key in CONSUMER_ENV_KEYS:
        if key in explicit:
            merged[key] = desired.get(key)
            continue
        if key in existing:
            merged[key] = existing[key]
            continue
        merged[key] = desired.get(key)
    return merged


def write_consumer_env(path: Path, updates: dict[str, str | None]) -> None:
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    rows = parse_env_file(original) if original else []
    seen: set[str] = set()
    lines: list[str] = []
    for key, value, raw in rows:
        if key in updates:
            if key in seen:
                continue
            seen.add(key)
            replacement = updates[key]
            if replacement is None or replacement == "":
                continue
            if value is not None and replacement == value:
                lines.append(raw)
                continue
            lines.append(f"{key}={encode_env_value(replacement)}")
            continue
        lines.append(raw)
    for key in CONSUMER_ENV_KEYS:
        if key in seen:
            continue
        replacement = updates.get(key)
        if replacement is None or replacement == "":
            continue
        lines.append(f"{key}={encode_env_value(replacement)}")
        seen.add(key)
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def apply_clone_env(
    clone: Path,
    *,
    inventory_files: list[Path],
    host_pool_files: list[Path],
    bootstrap_command: str,
    explicit: set[str],
) -> dict[str, str | None]:
    env_path = clone / ".env"
    existing = existing_env_map(env_path)
    desired: dict[str, str | None] = {
        "NFM_INVENTORY_FILES": join_path_list(inventory_files) if inventory_files else None,
        "NFM_HOST_POOL_FILES": join_path_list(host_pool_files) if host_pool_files else None,
        "NFM_BOOTSTRAP_COMMAND": bootstrap_command or None,
    }
    merged = merge_consumer_env(existing, desired, explicit=explicit)
    write_consumer_env(env_path, merged)
    progress("Updated consumer-owned NFM keys in the clone .env")
    return merged


def build_if_needed(clone: Path, commit: str) -> bool:
    marker = clone / "data" / ".deployed-commit"
    current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    ready = (clone / "node_modules").is_dir() and (clone / "dist/client").is_dir()
    if current == commit and ready:
        progress("Locked build already matches the selected commit")
        return False

    version = run(["node", "--version"]).stdout.strip().lstrip("v")
    try:
        major = int(version.split(".", 1)[0])
    except ValueError as exc:
        raise MonitorError(f"cannot parse Node.js version: {version}") from exc
    if major < 22:
        raise MonitorError(f"Node.js 22+ is required, found {version}")

    progress("Installing locked frontend dependencies")
    run(["npm", "ci"], cwd=clone, relay=True)
    progress("Running backend tests")
    run(["npm", "run", "test:backend"], cwd=clone, relay=True)
    progress("Building the production dashboard")
    run(["npm", "run", "build"], cwd=clone, relay=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(commit + "\n", encoding="utf-8")
    return True


def systemd_properties() -> dict[str, str]:
    result = run(
        [
            "systemctl",
            "--user",
            "show",
            "npu-fleet-monitor.service",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "UnitFileState",
        ],
        check=False,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if result.returncode != 0 and not values:
        values["error"] = (result.stderr or "systemctl query failed").strip()[-1000:]
    return values


def health(wait_seconds: float = 0) -> tuple[bool, dict[str, Any] | None, str | None]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + wait_seconds
    error = "health endpoint unavailable"
    while True:
        try:
            with opener.open(DEFAULT_URL, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "ok", payload, None
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            error = str(exc)
        if time.monotonic() >= deadline:
            return False, None, error
        time.sleep(0.5)


def install_and_restart(clone: Path) -> None:
    progress("Installing and enabling the user service")
    run([str(clone / INSTALL_ENTRY)], cwd=clone, relay=True)
    run(["systemctl", "--user", "restart", "npu-fleet-monitor.service"])


def payload_for(
    action: str,
    clone: Path | None,
    commit: str | None,
    built: bool | None,
    *,
    pin: dict[str, Any] | None = None,
    root_source: str | None = None,
    dirty: bool | None = None,
) -> dict[str, Any]:
    ok, health_payload, health_error = health()
    return {
        "ok": ok,
        "action": action,
        "allocation_authority": False,
        "clone": str(clone) if clone else None,
        "source_path": str(clone) if clone else None,
        "repository": CANONICAL_REPO,
        "ref": None if pin is None else pin.get("ref"),
        "commit": commit,
        "pinned_commit": None if pin is None else pin.get("commit"),
        "pin_matches": None if pin is None or commit is None else commit == pin.get("commit"),
        "root_source": root_source,
        "agent_skill": str(clone / AGENT_SKILL) if clone else None,
        "cli": str(clone / CLI_ENTRY) if clone else None,
        "mcp": str(clone / MCP_ENTRY) if clone else None,
        "start": str(clone / START_ENTRY) if clone else None,
        "install_user_service": str(clone / INSTALL_ENTRY) if clone else None,
        "service": systemd_properties(),
        "url": DASHBOARD_URL,
        "health": health_payload,
        "health_error": health_error,
        "built": built,
        "dirty": dirty,
    }


def _assert_readonly_command(command: list[str]) -> None:
    if not command:
        return
    name = Path(command[0]).name
    if name == "git":
        verbs = {item for item in command[1:] if not item.startswith("-") and item not in {"-C"}}
        # After git -C <path>, the verb is the first non-option token that is not the path.
        verb = None
        index = 1
        while index < len(command):
            item = command[index]
            if item == "-C":
                index += 2
                continue
            if item.startswith("-"):
                index += 1
                continue
            verb = item
            break
        if verb in MUTATING_GIT:
            raise MonitorError(f"read-only monitor action attempted mutating git {verb}")
        return
    if name in {"npm", "npm.cmd", "npm.exe", "node"}:
        raise MonitorError("read-only monitor action attempted an install or build")
    if name.endswith("install-user-service.sh"):
        raise MonitorError("read-only monitor action attempted user-service installation")


def resolve_inputs(args: argparse.Namespace, repo_root: Path | None = None) -> tuple[list[Path], list[Path], str, set[str]]:
    repo_root = REPO_ROOT if repo_root is None else repo_root
    explicit: set[str] = set()
    if args.inventory_files is not None:
        inventory = parse_path_list(args.inventory_files, label="NFM_INVENTORY_FILES")
        explicit.add("NFM_INVENTORY_FILES")
    else:
        inventory = default_inventory_files(repo_root)
    if args.host_pool_files is not None:
        host_pool = parse_path_list(args.host_pool_files, label="NFM_HOST_POOL_FILES")
        explicit.add("NFM_HOST_POOL_FILES")
    else:
        host_pool = default_host_pool_files(repo_root)
    if args.bootstrap_command is not None:
        if "\n" in args.bootstrap_command or "\r" in args.bootstrap_command:
            raise MonitorError("bootstrap command contains a newline and will not be written")
        bootstrap = args.bootstrap_command
        explicit.add("NFM_BOOTSTRAP_COMMAND")
    else:
        bootstrap = default_bootstrap_command(repo_root)
    return inventory, host_pool, bootstrap, explicit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Locate or deploy the local NPU fleet monitor from the standalone published repository"
    )
    parser.add_argument("action", choices=("ensure", "status", "restart", "stop"))
    parser.add_argument("--clone-dir", type=Path)
    parser.add_argument("--repo-url")
    parser.add_argument("--ref")
    parser.add_argument("--commit")
    parser.add_argument("--inventory-files")
    parser.add_argument("--host-pool-files")
    parser.add_argument("--bootstrap-command")
    args = parser.parse_args(argv)

    global _READ_ONLY_ACTIONS
    _READ_ONLY_ACTIONS = args.action != "ensure"
    try:
        pin = load_pin()
        if args.commit:
            if not _looks_like_commit(args.commit.strip()):
                raise MonitorError(f"explicit --commit is not a 40-character SHA: {args.commit}")
            pin = dict(pin)
            pin["commit"] = args.commit.strip()
        if args.ref:
            pin = dict(pin)
            pin["ref"] = args.ref
        url = require_canonical_url(args.repo_url or str(pin["url"]), what="repository URL")
        dest, root_source = resolve_clone_dir(args.clone_dir)
        created = False
        if args.action == "ensure":
            clone, created = locate_checkout(dest, create=True, pin=pin, url=url)
            commit = validate_existing_checkout(clone, pin, require_clean_for_ensure=not created)
            inventory, host_pool, bootstrap, explicit = resolve_inputs(args)
            apply_clone_env(
                clone,
                inventory_files=inventory,
                host_pool_files=host_pool,
                bootstrap_command=bootstrap,
                explicit=explicit,
            )
            built = build_if_needed(clone, commit)
            install_and_restart(clone)
            ok, health_payload, health_error = health(wait_seconds=30)
            result = payload_for(
                args.action,
                clone,
                commit,
                built,
                pin=pin,
                root_source=root_source,
                dirty=False,
            )
            result.update({"ok": ok, "health": health_payload, "health_error": health_error, "created": created})
        else:
            clone, _created = locate_checkout(dest, create=False, pin=pin, url=url)
            commit = validate_existing_checkout(clone, pin, require_clean_for_ensure=False)
            dirty = bool(git_source_dirty(clone))
            if args.action == "restart":
                run(["systemctl", "--user", "restart", "npu-fleet-monitor.service"])
                ok, health_payload, health_error = health(wait_seconds=30)
                result = payload_for(
                    args.action, clone, commit, None, pin=pin, root_source=root_source, dirty=dirty
                )
                result.update({"ok": ok, "health": health_payload, "health_error": health_error})
            elif args.action == "stop":
                run(["systemctl", "--user", "stop", "npu-fleet-monitor.service"])
                result = payload_for(
                    args.action, clone, commit, None, pin=pin, root_source=root_source, dirty=dirty
                )
                result["ok"] = result["service"].get("ActiveState") == "inactive"
            else:
                result = payload_for(
                    args.action, clone, commit, None, pin=pin, root_source=root_source, dirty=dirty
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 1
    except (MonitorError, OSError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": args.action,
                    "allocation_authority": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    finally:
        _READ_ONLY_ACTIONS = False


if __name__ == "__main__":
    raise SystemExit(main())
