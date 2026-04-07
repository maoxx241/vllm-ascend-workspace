#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

STATE_SUBDIR = Path('.vaws-local/remote-code-parity')
LEGACY_STATE_DIR = Path('.vaws-local')

DEFAULT_DENYLIST = (
    '.vaws-local/',
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

HARD_MIN_FREE_BYTES = 512 * 1024 * 1024
FIRST_INSTALL_MIN_FREE_BYTES = 4 * 1024 * 1024 * 1024
PROGRESS_SENTINEL = '__VAWS_PARITY_PROGRESS__='
STATE_LOCK_SUFFIX = '.lock'
DEFAULT_STATE_LOCK_TIMEOUT_SECONDS = 15.0
DEFAULT_STATE_LOCK_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    user: str

    def destination(self) -> str:
        return f'{self.user}@{self.host}'


@dataclass(frozen=True)
class SshStreamingResult:
    returncode: int
    stdout: str
    stderr: str
    progress_events: list[dict[str, Any]]


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=False,
        capture_output=capture_output,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(shlex.quote(part) for part in cmd)}\n"
            f'stdout:\n{result.stdout}\n'
            f'stderr:\n{result.stderr}'
        )
    return result


def git(repo: Path, args: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(['git', '-C', str(repo), *args], env=env, check=check)


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


def legacy_state_path(repo_root: Path, filename: str) -> Path:
    return repo_root / LEGACY_STATE_DIR / filename


def load_state(repo_root: Path, filename: str, default: Any) -> Any:
    canonical = canonical_state_path(repo_root, filename)
    if canonical.exists():
        return json.loads(canonical.read_text(encoding='utf-8'))
    legacy = legacy_state_path(repo_root, filename)
    if legacy.exists():
        return json.loads(legacy.read_text(encoding='utf-8'))
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
    legacy = legacy_state_path(repo_root, filename)
    if legacy != path:
        legacy.unlink(missing_ok=True)
    return path


@contextlib.contextmanager
def state_lock(
    repo_root: Path,
    filename: str,
    *,
    timeout_seconds: float = DEFAULT_STATE_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_STATE_LOCK_POLL_SECONDS,
):
    lock_path = canonical_state_path(repo_root, filename + STATE_LOCK_SUFFIX)
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f'{os.getpid()}\n'.encode('utf-8'))
            break
        except FileExistsError:
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


def human_bytes(value: int) -> str:
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f'{size:.1f} {unit}'
        size /= 1024.0
    return f'{value} B'


def quoted(script: str) -> str:
    return shlex.quote(script)


def _ssh_base_cmd(endpoint: SshEndpoint) -> list[str]:
    return [
        'ssh',
        '-o',
        'BatchMode=yes',
        '-o',
        'StrictHostKeyChecking=accept-new',
        '-o',
        'LogLevel=ERROR',
        '-p',
        str(endpoint.port),
        endpoint.destination(),
    ]


def parse_progress_event(line: str) -> dict[str, Any] | None:
    if not line.startswith(PROGRESS_SENTINEL):
        return None
    try:
        payload = json.loads(line[len(PROGRESS_SENTINEL) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = [*_ssh_base_cmd(endpoint), 'bash', '-c', shlex.quote(script)]
    return run(cmd, check=check, capture_output=capture_output)


def ssh_exec_stream(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    stream_progress: bool = True,
) -> SshStreamingResult:
    cmd = [*_ssh_base_cmd(endpoint), 'bash', '-c', shlex.quote(script)]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
    )

    assert proc.stdout is not None
    assert proc.stderr is not None

    q: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    progress_events: list[dict[str, Any]] = []

    def reader(stream_name: str, pipe: Any) -> None:
        try:
            for line in pipe:
                q.put((stream_name, line))
        finally:
            q.put((stream_name, None))

    threads = [
        threading.Thread(target=reader, args=('stdout', proc.stdout), daemon=True),
        threading.Thread(target=reader, args=('stderr', proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    done_streams: set[str] = set()
    while len(done_streams) < 2 or proc.poll() is None:
        try:
            stream_name, line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            done_streams.add(stream_name)
            continue
        if stream_name == 'stdout':
            stdout_parts.append(line)
            continue
        event = parse_progress_event(line)
        if event is not None:
            progress_events.append(event)
            if stream_progress:
                sys.stderr.write(line if line.endswith('\n') else line + '\n')
                sys.stderr.flush()
            continue
        stderr_parts.append(line)

    returncode = proc.wait()
    for thread in threads:
        thread.join(timeout=1)

    while True:
        try:
            stream_name, line = q.get_nowait()
        except queue.Empty:
            break
        if line is None:
            continue
        if stream_name == 'stdout':
            stdout_parts.append(line)
            continue
        event = parse_progress_event(line)
        if event is not None:
            if event not in progress_events:
                progress_events.append(event)
            if stream_progress:
                sys.stderr.write(line if line.endswith('\n') else line + '\n')
                sys.stderr.flush()
            continue
        stderr_parts.append(line)

    stdout = ''.join(stdout_parts)
    stderr = ''.join(stderr_parts)
    if check and returncode != 0:
        rendered_cmd = ' '.join(shlex.quote(part) for part in cmd)
        raise RuntimeError(
            f'command failed ({returncode}): {rendered_cmd}\n'
            f'stdout:\n{stdout}\n'
            f'stderr:\n{stderr}'
        )
    return SshStreamingResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        progress_events=progress_events,
    )


def ssh_stream_to_file(endpoint: SshEndpoint, remote_path: str, payload: str) -> None:
    script = f'mkdir -p {quoted(str(Path(remote_path).parent))} && cat > {quoted(remote_path)}'
    cmd = [*_ssh_base_cmd(endpoint), 'bash', '-c', shlex.quote(script)]
    result = subprocess.run(cmd, input=payload, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f'failed to stream payload to {remote_path}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
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
    name = git(repo, ['config', '--get', 'user.name'], check=False).stdout.strip() or None
    email = git(repo, ['config', '--get', 'user.email'], check=False).stdout.strip() or None
    return name, email


def glob_match_any(path: str, patterns: Iterable[str]) -> bool:
    import fnmatch

    normalized = path.replace('\\', '/')
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)
