#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_LIB_DIR = Path(__file__).resolve().parents[4] / '.agents' / 'lib'
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from vaws_remote_dev import ssh_exec as remote_ssh_exec, ssh_run_bytes  # noqa: E402
from vaws_result_envelope import emit_skill_json  # noqa: E402

WORKSPACE_ID_PATTERN = re.compile(r'[^A-Za-z0-9._-]+')
STATE_SUBDIR = Path('.vaws-local/remote-code-parity')
DEFAULT_DENYLIST = (
    '.vaws-local/',
    '.vaws-runtime/',
    '.remote-code-parity/',
    '.workspace.local/',
    '.machine-inventory.json',
    '.codex/',
    '.claude/settings.local.json',
    '.env',
    '.env.*',
    '.venv/',
    'venv/',
    '__pycache__/',
    '.pytest_cache/',
    '.mypy_cache/',
    '.ruff_cache/',
    '*.log',
    '*.out',
    '.DS_Store',
    '._*',
    'Thumbs.db',
)

STATE_LOCK_SUFFIX = '.lock'
DEFAULT_STATE_LOCK_TIMEOUT_SECONDS = 15.0
DEFAULT_STATE_LOCK_POLL_SECONDS = 0.05
DEFAULT_STATE_LOCK_STALE_SECONDS = 60 * 60 * 6


@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    user: str

    def destination(self) -> str:
        return f'{self.user}@{self.host}'


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=False,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"command timed out after {timeout:.0f}s: "
            f"{' '.join(shlex.quote(part) for part in cmd)}\n"
            f'stdout:\n{exc.stdout or ""}\n'
            f'stderr:\n{exc.stderr or ""}'
        ) from exc
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(shlex.quote(part) for part in cmd)}\n"
            f'stdout:\n{result.stdout}\n'
            f'stderr:\n{result.stderr}'
        )
    return result


def git(
    repo: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(['git', '-C', str(repo), *args], env=env, check=check, timeout=timeout)


def repo_root_from(path: Path) -> Path:
    current = path.resolve()
    while True:
        if (current / '.git').exists():
            return current
        if current.parent == current:
            raise RuntimeError(f'could not find git repo root above {path}')
        current = current.parent


def state_dir(repo_root: Path) -> Path:
    target = repo_root / STATE_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def canonical_state_path(repo_root: Path, filename: str) -> Path:
    return state_dir(repo_root) / filename


def load_state(repo_root: Path, filename: str, default: Any) -> Any:
    canonical = canonical_state_path(repo_root, filename)
    if canonical.exists():
        return json.loads(canonical.read_text(encoding='utf-8'))
    return default


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def save_state(repo_root: Path, filename: str, data: Any) -> Path:
    path = canonical_state_path(repo_root, filename)
    _atomic_write_json(path, data)
    return path


@contextlib.contextmanager
def state_lock(
    repo_root: Path,
    filename: str,
    *,
    timeout_seconds: float = DEFAULT_STATE_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_STATE_LOCK_POLL_SECONDS,
    stale_after_seconds: float = DEFAULT_STATE_LOCK_STALE_SECONDS,
):
    lock_path = canonical_state_path(repo_root, filename + STATE_LOCK_SUFFIX)
    deadline = time.monotonic() + timeout_seconds
    owner = {
        "pid": os.getpid(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else None,
        "created_at": now_utc(),
    }
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, (json.dumps(owner, sort_keys=True) + "\n").encode('utf-8'))
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age >= stale_after_seconds:
                with contextlib.suppress(FileNotFoundError):
                    lock_path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f'timed out waiting for state lock {lock_path}')
            time.sleep(poll_seconds)
    try:
        yield lock_path
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def update_state(repo_root: Path, filename: str, default: Any, updater: Any) -> tuple[Any, Path, Any]:
    with state_lock(repo_root, filename):
        state = load_state(repo_root, filename, default)
        result = updater(state)
        path = save_state(repo_root, filename, state)
    return state, path, result


def now_utc() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def sanitize_repo_id(relpath: str) -> str:
    return 'workspace' if relpath in ('', '.') else relpath.replace('/', '__')


def json_dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def print_json(data: dict[str, Any]) -> None:
    emit_skill_json(
        data,
        skill="remote-code-parity",
        entry_point=".agents/skills/remote-code-parity/scripts/parity_sync.py",
    )


def quoted(script: str) -> str:
    return shlex.quote(script)


def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    del capture_output
    return remote_ssh_exec(endpoint, script, check=check)


def ssh_stream_to_file(endpoint: SshEndpoint, remote_path: str, payload: str) -> None:
    script = f'mkdir -p {quoted(str(Path(remote_path).parent))} && cat > {quoted(remote_path)}'
    result = ssh_run_bytes(endpoint, script, stdin=payload.encode())
    if result.returncode != 0:
        raise RuntimeError(
            f'failed to stream payload to {remote_path}\n'
            f'stdout:\n{result.stdout.decode("utf-8", errors="replace")}\n'
            f'stderr:\n{result.stderr.decode("utf-8", errors="replace")}'
        )


def ssh_stream_bytes_to_file(endpoint: SshEndpoint, remote_path: str, payload: bytes) -> None:
    script = (
        f'mkdir -p {quoted(str(Path(remote_path).parent))} && '
        f'head -c {len(payload)} > {quoted(remote_path)}'
    )
    result = ssh_run_bytes(endpoint, script, stdin=payload)
    if result.returncode != 0:
        raise RuntimeError(
            f'failed to stream binary payload to {remote_path}\n'
            f'stdout:\n{result.stdout.decode("utf-8", errors="replace")}\n'
            f'stderr:\n{result.stderr.decode("utf-8", errors="replace")}'
        )


def is_git_worktree(path: Path) -> bool:
    result = git(path, ['rev-parse', '--is-inside-work-tree'], check=False)
    if result.returncode != 0 or result.stdout.strip() != 'true':
        return False
    top = git(path, ['rev-parse', '--show-toplevel'], check=False)
    if top.returncode != 0:
        return False
    try:
        return Path(top.stdout.strip()).resolve() == path.resolve()
    except FileNotFoundError:
        return False


def ensure_local_git_identity(repo: Path) -> tuple[str | None, str | None]:
    # Despite the name this is read-only: it never sets git config, it only
    # reports the identity (if any) that snapshot commits would record.
    name = git(repo, ['config', '--get', 'user.name'], check=False).stdout.strip() or None
    email = git(repo, ['config', '--get', 'user.email'], check=False).stdout.strip() or None
    return name, email


def glob_match_any(path: str, patterns: Iterable[str]) -> bool:
    import fnmatch

    normalized = path.replace('\\', '/')
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)
