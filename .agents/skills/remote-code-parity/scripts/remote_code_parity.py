#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from common import (
    DEFAULT_DENYLIST,
    PROGRESS_SENTINEL,
    WORKSPACE_ID_PATTERN,
    SshEndpoint,
    ensure_local_git_identity,
    git,
    glob_match_any,
    is_git_worktree,
    json_dump,
    load_state,
    now_utc,
    quoted,
    repo_root_from,
    sanitize_repo_id,
    save_state,
    ssh_exec,
    ssh_exec_stream,
    ssh_stream_bytes_to_file,
    ssh_stream_to_file,
    update_state,
)


VLLM_REINSTALL_PATTERNS = (
    'requirements*',
    'pyproject.toml',
    'setup.py',
    'setup.cfg',
    'CMakeLists.txt',
    'cmake/**',
    'csrc/**',
    '**/*.cu',
    '**/*.cuh',
    '**/*.cpp',
    '**/*.cc',
    '**/*.h',
    '**/*.hpp',
)

VLLM_ASCEND_REINSTALL_PATTERNS = VLLM_REINSTALL_PATTERNS + (
    'vllm_ascend/_cann_ops_custom/**',
)

DEPENDENCY_INSTALL_PATTERNS = (
    'requirements*',
    'pyproject.toml',
    'setup.py',
    'setup.cfg',
)

DEFAULT_ENV_PREAMBLE = (
    'export PATH="${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"',
    'export VAWS_RUNTIME_ROOT="${VAWS_RUNTIME_ROOT:-/vllm-workspace}"',
    'prepend_ld_path() {',
    '  dir="$1"',
    '  if [ -d "$dir" ]; then',
    '    export LD_LIBRARY_PATH="$dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"',
    '  fi',
    '}',
    'prepend_ld_path /usr/local/Ascend/driver/lib64',
    'prepend_ld_path /usr/local/Ascend/driver/lib64/driver',
    'safe_source() {',
    '  file="$1"',
    '  if [ -f "$file" ]; then',
    '    set +u',
    '    source "$file" >/dev/null 2>&1 || true',
    '    set -u',
    '  fi',
    '}',
    'for _ascend_env in '
    '/etc/profile.d/vaws-ascend-env.sh '
    '/usr/local/Ascend/cann-*/set_env.sh '
    '/usr/local/Ascend/ascend-toolkit/latest/set_env.sh '
    '/usr/local/Ascend/ascend-toolkit/set_env.sh '
    '/usr/local/Ascend/nnal/atb/set_env.sh '
    '"$VAWS_RUNTIME_ROOT/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash"; do',
    '  safe_source "$_ascend_env"',
    'done',
    'for _ascend_lib in '
    '/usr/local/Ascend/cann-*/lib64 '
    '/usr/local/Ascend/cann-*/runtime/lib64 '
    '/usr/local/Ascend/ascend-toolkit/latest/lib64 '
    '/usr/local/Ascend/ascend-toolkit/lib64; do',
    '  prepend_ld_path "$_ascend_lib"',
    'done',
    'unset _ascend_env _ascend_lib',
    'PYTHON_CANDIDATE="$(ls -1d /usr/local/python*/bin/python3 2>/dev/null | sort -V | tail -n 1 || true)"',
    'if [ -n "$PYTHON_CANDIDATE" ]; then export PYTHON="$PYTHON_CANDIDATE"; elif command -v python3 >/dev/null 2>&1; then export PYTHON="$(command -v python3)"; elif command -v python >/dev/null 2>&1; then export PYTHON="$(command -v python)"; else echo "python not found" >&2; exit 127; fi',
    'PYTHON_BIN_DIR="$(dirname "$PYTHON")"',
    'VAWS_PYTHON_SHIM_DIR="$(mktemp -d /tmp/vaws-python-shim.XXXXXX)"',
    'trap "rm -rf \"$VAWS_PYTHON_SHIM_DIR\"" EXIT',
    'ln -sf "$PYTHON" "$VAWS_PYTHON_SHIM_DIR/python"',
    'ln -sf "$PYTHON" "$VAWS_PYTHON_SHIM_DIR/python3"',
    'export PATH="$VAWS_PYTHON_SHIM_DIR:$PYTHON_BIN_DIR:$PATH"',
    'hash -r',
    'export HI_PYTHON="$PYTHON"',
    'export Python3_EXECUTABLE="$PYTHON"',
    'export Python_EXECUTABLE="$PYTHON"',
    'export CMAKE_ARGS="-DPython3_EXECUTABLE=$PYTHON -DPython_EXECUTABLE=$PYTHON ${CMAKE_ARGS:-}"',
    'if [ -z "${VAWS_BUILD_JOBS:-}" ]; then',
    '  VAWS_BUILD_JOBS="$("$PYTHON" - <<\'PY\'',
    'import os',
    'try:',
    '    count = len(os.sched_getaffinity(0))',
    'except Exception:',
    '    count = os.cpu_count() or 1',
    'print(max(1, min(int(count), 128)))',
    'PY',
    ')"',
    'fi',
    'export VAWS_BUILD_JOBS',
    'export MAX_JOBS="${MAX_JOBS:-$VAWS_BUILD_JOBS}"',
    'export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-$VAWS_BUILD_JOBS}"',
    'export PIP="$PYTHON -m pip"',
    'export PIP_DISABLE_PIP_VERSION_CHECK=1',
    'export PIP_NO_INPUT=1',
    'export PIP_DEFAULT_TIMEOUT=60',
    'export PIP_RETRIES=1',
    'export PIP_PROGRESS_BAR=off',
    'export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/root/.cache}"',
    'export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$XDG_CACHE_HOME/pip}"',
    'export FETCHCONTENT_BASE_DIR="${FETCHCONTENT_BASE_DIR:-$XDG_CACHE_HOME/vaws/fetchcontent}"',
    'export PIP_CONFIG_FILE=/dev/null',
    'unset PIP_EXTRA_INDEX_URL',
    'export PIP_INDEX_URL="https://repo.huaweicloud.com/repository/pypi/simple"',
    'export PIP_TRUSTED_HOST="repo.huaweicloud.com"',
    'export CMAKE_BUILD_TYPE="${CMAKE_BUILD_TYPE:-Release}"',
    'if [ -n "${VAWS_SOC_VERSION:-}" ]; then export SOC_VERSION="$VAWS_SOC_VERSION"; fi',
    'if [ -n "${VAWS_COMPILE_CUSTOM_KERNELS:-}" ]; then export COMPILE_CUSTOM_KERNELS="$VAWS_COMPILE_CUSTOM_KERNELS"; fi',
    'if [ "${VAWS_USE_CLANG15:-0}" = "1" ] && command -v clang-15 >/dev/null 2>&1 && command -v clang++-15 >/dev/null 2>&1; then export C_COMPILER="${C_COMPILER:-$(command -v clang-15)}"; export CXX_COMPILER="${CXX_COMPILER:-$(command -v clang++-15)}"; fi',
    'export VLLM_WORKER_MULTIPROC_METHOD=spawn',
    'export OMP_NUM_THREADS=1',
    'export MKL_NUM_THREADS=1',
)

PIP_INDEX_NAME = 'huaweicloud'
PIP_INDEX_URL = 'https://repo.huaweicloud.com/repository/pypi/simple'
PIP_TRUSTED_HOST = 'repo.huaweicloud.com'

DEFAULT_CONTAINER_CACHE_ROOT = '/root/.cache/vaws/remote-code-parity'
DEFAULT_MARKER_DIRNAME = '.remote-code-parity'
DEFAULT_GIT_TRANSPORT_TIMEOUT_SECONDS = 900.0
DEFAULT_CONTAINER_LOCK_STALE_SECONDS = 3600
# Keep runtime-private state and profiling artifacts that may be needed for
# post-run analysis across parity refreshes.
DEFAULT_ROOT_PRESERVE_PATHS = ('Mooncake', '.vaws-runtime')
STATE_FILENAME = 'runtime-state.json'
CONSENT_FILENAME = 'install-consents.json'
PARITY_BRANCH_NAME = 'parity-current'

REMOTE_RUNTIME_ENV_PASSTHROUGH = (
    'XDG_CACHE_HOME',
    'PIP_CACHE_DIR',
    'FETCHCONTENT_BASE_DIR',
    'VAWS_BUILD_JOBS',
    'MAX_JOBS',
    'CMAKE_BUILD_PARALLEL_LEVEL',
    'CMAKE_BUILD_TYPE',
    'VAWS_SOC_VERSION',
    'SOC_VERSION',
    'VAWS_COMPILE_CUSTOM_KERNELS',
    'COMPILE_CUSTOM_KERNELS',
    'VAWS_USE_CLANG15',
    'C_COMPILER',
    'CXX_COMPILER',
    'VERBOSE',
    'ASCEND_HOME_PATH',
)

RUNTIME_INSTALL_ENV_KEYS = (
    'MAX_JOBS',
    'CMAKE_BUILD_PARALLEL_LEVEL',
    'CMAKE_BUILD_TYPE',
    'FETCHCONTENT_BASE_DIR',
    'XDG_CACHE_HOME',
    'PIP_CACHE_DIR',
    'PIP_CONFIG_FILE',
    'PIP_INDEX_URL',
    'PIP_TRUSTED_HOST',
    'VAWS_BUILD_JOBS',
    'SOC_VERSION',
    'COMPILE_CUSTOM_KERNELS',
    'C_COMPILER',
    'CXX_COMPILER',
    'ASCEND_HOME_PATH',
)


@dataclass
class SubmoduleEntry:
    name: str
    path: str


@dataclass
class RepoNode:
    relpath: str
    repo_path: Path
    submodule_name: str | None
    children: list['RepoNode'] = field(default_factory=list)


@dataclass
class SnapshotRecord:
    relpath: str
    repo_id: str
    source_head: str | None
    parent: str | None
    commit: str
    tree: str
    ref: str
    changed_paths: list[str]
    submodules: list[dict[str, str]]


def normalize_workspace_id(value: str) -> str:
    cleaned = WORKSPACE_ID_PATTERN.sub('-', value).strip('.-')
    return cleaned or 'workspace'


def validate_relative_posix_path(value: str, *, label: str) -> str:
    candidate = PurePosixPath(value)
    if not value or value in ('.', '..'):
        raise RuntimeError(f'{label} must not be empty')
    if candidate.is_absolute():
        raise RuntimeError(f'{label} must be relative, got: {value!r}')
    if '..' in candidate.parts:
        raise RuntimeError(f'{label} must not contain parent traversal, got: {value!r}')
    normalized = candidate.as_posix()
    if normalized in ('.', ''):
        raise RuntimeError(f'{label} must not be empty')
    return normalized


def validate_absolute_posix_path(value: str, *, label: str) -> str:
    if not value.startswith('/'):
        raise RuntimeError(f'{label} must be an absolute POSIX path, got: {value!r}')
    return PurePosixPath(value).as_posix()


def remote_runtime_env_exports() -> list[str]:
    lines: list[str] = []
    for key in REMOTE_RUNTIME_ENV_PASSTHROUGH:
        if key in os.environ:
            lines.append(f'export {key}={quoted(os.environ[key])}')
    return lines


def redact_url_value(value: str) -> str:
    parts = value.split()
    redacted_parts: list[str] = []
    for part in parts:
        try:
            parsed = urlsplit(part)
        except ValueError:
            redacted_parts.append(part)
            continue
        if parsed.scheme and parsed.netloc and '@' in parsed.netloc:
            host = parsed.netloc.rsplit('@', 1)[1]
            redacted_parts.append(urlunsplit((parsed.scheme, f'***@{host}', parsed.path, parsed.query, parsed.fragment)))
        else:
            redacted_parts.append(part)
    return ' '.join(redacted_parts)


def redact_runtime_env(env: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in env.items():
        if key.endswith('_URL') or 'INDEX' in key or 'PATH' in key:
            redacted[key] = redact_url_value(value)
        else:
            redacted[key] = value
    return redacted


def resolved_root_preserve_paths(marker_dirname: str, extra_paths: list[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    for path in [*DEFAULT_ROOT_PRESERVE_PATHS, marker_dirname, *extra_paths]:
        normalized = validate_relative_posix_path(path, label='preserve path')
        if normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered)


@dataclass
class RuntimeInstallMarker:
    path: str
    record: dict[str, Any] | None


def emit_progress(phase: str, **fields: Any) -> None:
    payload = {'phase': phase, **fields}
    print(f'{PROGRESS_SENTINEL}{json.dumps(payload, ensure_ascii=False)}', file=sys.stderr, flush=True)


def ensure_populated_worktree(repo: Path, relpath: str) -> None:
    if not repo.exists():
        raise RuntimeError(
            f'required repo path {relpath} is missing; initialize submodules before remote-code-parity'
        )
    if not is_git_worktree(repo):
        raise RuntimeError(
            f'required repo path {relpath} is not a populated Git worktree; run repo-init or git submodule update --init --recursive before remote-code-parity'
        )


def list_submodules(repo: Path) -> list[SubmoduleEntry]:
    gitmodules = repo / '.gitmodules'
    if not gitmodules.exists():
        return []
    result = git(repo, ['config', '--file', '.gitmodules', '--get-regexp', r'^submodule\..*\.path$'], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    entries: list[SubmoduleEntry] = []
    for line in result.stdout.splitlines():
        key, path = line.split(maxsplit=1)
        name = key.removeprefix('submodule.').removesuffix('.path')
        entries.append(SubmoduleEntry(name=name, path=path.strip()))
    return entries


def discover_repo_tree(repo: Path, relpath: str = '.', submodule_name: str | None = None) -> RepoNode:
    ensure_populated_worktree(repo, relpath)
    node = RepoNode(relpath=relpath, repo_path=repo, submodule_name=submodule_name)
    for entry in list_submodules(repo):
        child_repo = repo / entry.path
        child_relpath = entry.path if relpath in ('', '.') else f'{relpath}/{entry.path}'
        ensure_populated_worktree(child_repo, child_relpath)
        node.children.append(discover_repo_tree(child_repo, child_relpath, entry.name))
    return node


def iter_postorder(node: RepoNode):
    for child in node.children:
        yield from iter_postorder(child)
    yield node


def git_head(repo: Path) -> str | None:
    result = git(repo, ['rev-parse', '--verify', 'HEAD'], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_tree_for_commit(repo: Path, commit: str | None) -> str | None:
    if not commit:
        return None
    result = git(repo, ['rev-parse', f'{commit}^{{tree}}'], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def reset_pathspecs(node: RepoNode, denylist: tuple[str, ...]) -> list[str]:
    specs: list[str] = []
    for child in node.children:
        specs.append(child.repo_path.relative_to(node.repo_path).as_posix())
    for pattern in denylist:
        if any(ch in pattern for ch in '*?[]'):
            specs.append(f':(glob){pattern}')
        else:
            specs.append(pattern)
    return specs


def synthetic_ref(workspace_id: str, snapshot_id: str, relpath: str) -> str:
    return f'refs/parity/{workspace_id}/{snapshot_id}/{sanitize_repo_id(relpath)}'


def commit_message(workspace_id: str, relpath: str) -> str:
    return f'remote-code-parity tree snapshot {workspace_id} {sanitize_repo_id(relpath)}'


def gitlink_for_path(repo: Path, commit: str | None, path: str) -> str | None:
    if not commit:
        return None
    result = git(repo, ['ls-tree', commit, '--', path], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    first = result.stdout.splitlines()[0].split(maxsplit=3)
    if len(first) < 3 or first[0] != '160000':
        return None
    return first[2]


def filter_transport_only_child_paths(
    paths: list[str],
    transport_only_child_paths: set[str],
) -> list[str]:
    return [path for path in paths if path not in transport_only_child_paths]


def build_synthetic_snapshot(
    node: RepoNode,
    *,
    workspace_id: str,
    snapshot_id: str,
    denylist: tuple[str, ...],
    child_commits: dict[str, SnapshotRecord],
) -> SnapshotRecord:
    repo = node.repo_path
    source_head = git_head(repo)
    ref = synthetic_ref(workspace_id, snapshot_id, node.relpath)
    temp_index = tempfile.NamedTemporaryFile(prefix='parity-index-', delete=False)
    temp_index.close()
    temp_index_path = Path(temp_index.name)
    env = os.environ.copy()
    env['GIT_INDEX_FILE'] = temp_index.name
    env['GIT_OPTIONAL_LOCKS'] = '0'
    author_name, author_email = ensure_local_git_identity(repo)
    env.setdefault('GIT_AUTHOR_NAME', author_name or 'remote-code-parity')
    env.setdefault('GIT_AUTHOR_EMAIL', author_email or 'remote-code-parity@example.invalid')
    env.setdefault('GIT_AUTHOR_DATE', '1970-01-01T00:00:00Z')
    env.setdefault('GIT_COMMITTER_NAME', author_name or 'remote-code-parity')
    env.setdefault('GIT_COMMITTER_EMAIL', author_email or 'remote-code-parity@example.invalid')
    env.setdefault('GIT_COMMITTER_DATE', '1970-01-01T00:00:00Z')

    try:
        if source_head:
            git(repo, ['read-tree', source_head], env=env)
        git(repo, ['add', '-A'], env=env)
        reset_specs = reset_pathspecs(node, denylist)
        if reset_specs:
            git(repo, ['reset', '-q', '--', *reset_specs], env=env)

        submodule_records: list[dict[str, str]] = []
        transport_only_child_paths: set[str] = set()
        for child in node.children:
            child_record = child_commits[child.relpath]
            child_rel_to_repo = child.repo_path.relative_to(repo).as_posix()
            source_gitlink = gitlink_for_path(repo, source_head, child_rel_to_repo)
            if (
                source_gitlink
                and child_record.source_head == source_gitlink
                and not child_record.changed_paths
            ):
                transport_only_child_paths.add(child_rel_to_repo)
            git(
                repo,
                ['update-index', '--add', '--cacheinfo', f'160000,{child_record.commit},{child_rel_to_repo}'],
                env=env,
            )
            submodule_records.append(
                {
                    'name': child.submodule_name or child_rel_to_repo,
                    'path': child_rel_to_repo,
                    'commit': child_record.commit,
                    'repo_id': child_record.repo_id,
                }
            )

        tree = git(repo, ['write-tree'], env=env).stdout.strip()
        commit = git(repo, ['commit-tree', tree, '-m', commit_message(workspace_id, node.relpath)], env=env).stdout.strip()
        if source_head:
            diff = git(repo, ['diff', '--name-only', f'{source_head}..{commit}']).stdout.splitlines()
        else:
            diff = git(repo, ['show', '--pretty=', '--name-only', commit]).stdout.splitlines()
        diff = filter_transport_only_child_paths(diff, transport_only_child_paths)

        git(repo, ['update-ref', ref, commit])

        return SnapshotRecord(
            relpath=node.relpath,
            repo_id=sanitize_repo_id(node.relpath),
            source_head=source_head,
            parent=source_head,
            commit=commit,
            tree=tree,
            ref=ref,
            changed_paths=[path.strip() for path in diff if path.strip()],
            submodules=submodule_records,
        )
    finally:
        temp_index_path.unlink(missing_ok=True)


def cleanup_synthetic_refs(workspace_root: Path, records: list[SnapshotRecord]) -> None:
    for record in records:
        repo = workspace_root if record.relpath in ('', '.') else workspace_root / record.relpath
        git(repo, ['update-ref', '-d', record.ref], check=False)


def load_runtime_state(repo_root: Path) -> dict[str, Any]:
    return load_state(repo_root, STATE_FILENAME, {'schema_version': 2, 'servers': {}})


def save_runtime_state(repo_root: Path, state: dict[str, Any]) -> Path:
    return save_state(repo_root, STATE_FILENAME, state)


def load_consent(repo_root: Path) -> dict[str, Any]:
    return load_state(repo_root, CONSENT_FILENAME, {'schema_version': 1, 'consents': {}})


def resolve_install_consent(repo_root: Path, server_name: str, container_identity: str) -> str:
    state = load_consent(repo_root)
    decision = (
        state.get('consents', {})
        .get(server_name, {})
        .get('containers', {})
        .get(container_identity, {})
        .get('decision')
    )
    return decision or 'unknown'


def cache_workspace_root(container_cache_root: str, workspace_id: str) -> str:
    return f"{container_cache_root.rstrip('/')}/workspaces/{workspace_id}"


def mirror_path_for(container_cache_root: str, workspace_id: str, record: SnapshotRecord) -> str:
    root = Path(cache_workspace_root(container_cache_root, workspace_id)) / 'mirrors'
    if record.repo_id == 'workspace':
        return str(root / 'workspace.git')
    return str(root / 'nested' / f'{record.repo_id}.git')


def bundle_path_for(container_cache_root: str, workspace_id: str, record: SnapshotRecord) -> str:
    root = Path(cache_workspace_root(container_cache_root, workspace_id)) / 'bundles'
    return str(root / f'{record.repo_id}-{record.commit}.bundle')


def manifest_path_for(container_cache_root: str, workspace_id: str, snapshot_id: str) -> str:
    return str(Path(cache_workspace_root(container_cache_root, workspace_id)) / 'manifests' / f'{snapshot_id}.json')


def lock_path_for(container_cache_root: str, workspace_id: str, container_identity: str) -> str:
    token = re.sub(r'[^A-Za-z0-9._-]+', '-', container_identity).strip('.-') or 'container'
    return str(Path(cache_workspace_root(container_cache_root, workspace_id)) / 'locks' / token)


def marker_path_for(runtime_root: str, marker_dirname: str) -> str:
    return str(Path(runtime_root) / marker_dirname / 'runtime-install.json')


def ensure_remote_bare_repos(container: SshEndpoint, mirror_paths: list[str], dry_run: bool) -> None:
    if dry_run or not mirror_paths:
        return
    lines = ['set -eo pipefail']
    for mirror_path in mirror_paths:
        lines.extend(
            [
                f'mkdir -p {quoted(str(Path(mirror_path).parent))}',
                f'if [ -e {quoted(mirror_path)} ] && [ ! -d {quoted(str(Path(mirror_path) / "objects"))} ]; then rm -rf {quoted(mirror_path)}; fi',
                f'if [ ! -d {quoted(mirror_path)} ]; then git init --bare {quoted(mirror_path)} >/dev/null; fi',
            ]
        )
    ssh_exec(container, '\n'.join(lines))


def cleanup_failed_mirror_hydration(container: SshEndpoint, mirror_path: str) -> None:
    script = '\n'.join(
        [
            'set +e',
            f'mirror={quoted(mirror_path)}',
            'for pid in $(pgrep -x git-receive-pack 2>/dev/null || true); do',
            '  cmd="$(tr "\\000" " " <"/proc/$pid/cmdline" 2>/dev/null || true)"',
            '  case "$cmd" in',
            '    *"$mirror"*)',
            '      pkill -TERM -P "$pid" >/dev/null 2>&1 || true',
            '      kill -TERM "$pid" >/dev/null 2>&1 || true',
            '      ;;',
            '  esac',
            'done',
            'sleep 1',
            'for pid in $(pgrep -x git-receive-pack 2>/dev/null || true); do',
            '  cmd="$(tr "\\000" " " <"/proc/$pid/cmdline" 2>/dev/null || true)"',
            '  case "$cmd" in',
            '    *"$mirror"*)',
            '      pkill -KILL -P "$pid" >/dev/null 2>&1 || true',
            '      kill -KILL "$pid" >/dev/null 2>&1 || true',
            '      ;;',
            '  esac',
            'done',
            'rm -rf "$mirror"',
        ]
    )
    try:
        ssh_exec(container, script, check=False)
    except Exception:
        pass


def push_snapshot_to_mirror(
    repo: Path,
    *,
    container: SshEndpoint,
    mirror_path: str,
    container_cache_root: str,
    record: SnapshotRecord,
    workspace_id: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    target_ref = f'refs/parity/{workspace_id}/current'
    remote_bundle_path = bundle_path_for(container_cache_root, workspace_id, record)
    local_bundle = tempfile.NamedTemporaryFile(prefix='parity-bundle-', suffix='.bundle', delete=False)
    local_bundle.close()
    local_bundle_path = Path(local_bundle.name)
    try:
        git(repo, ['bundle', 'create', str(local_bundle_path), record.ref], timeout=DEFAULT_GIT_TRANSPORT_TIMEOUT_SECONDS)
        ssh_stream_bytes_to_file(container, remote_bundle_path, local_bundle_path.read_bytes())
        script = '\n'.join(
            [
                'set -eo pipefail',
                f'mkdir -p {quoted(str(Path(mirror_path).parent))}',
                f'if [ ! -d {quoted(mirror_path)} ]; then git init --bare {quoted(mirror_path)} >/dev/null; fi',
                (
                    f'git -C {quoted(mirror_path)} fetch --force {quoted(remote_bundle_path)} '
                    f'{quoted(record.ref + ":" + target_ref)} '
                    f'{quoted(record.ref + ":refs/heads/" + PARITY_BRANCH_NAME)} >/dev/null'
                ),
                f'rm -f {quoted(remote_bundle_path)}',
            ]
        )
        ssh_exec(container, script)
    except Exception:
        cleanup_failed_mirror_hydration(container, mirror_path)
        raise
    finally:
        local_bundle_path.unlink(missing_ok=True)

def acquire_container_lock(
    container: SshEndpoint,
    lock_path: str,
    dry_run: bool,
    *,
    stale_seconds: int = DEFAULT_CONTAINER_LOCK_STALE_SECONDS,
) -> None:
    if dry_run:
        return
    script = '\n'.join(
        [
            'set -eo pipefail',
            f'lock={quoted(lock_path)}',
            f'stale_seconds={int(stale_seconds)}',
            'mkdir -p "$(dirname "$lock")"',
            'write_owner() {',
            '  {',
            '    printf "pid=%s\\n" "$$"',
            '    printf "host=%s\\n" "$(hostname 2>/dev/null || true)"',
            '    printf "started_at=%s\\n" "$(date -Is 2>/dev/null || date)"',
            '  } >"$lock/owner"',
            '}',
            'if mkdir "$lock" 2>/dev/null; then',
            '  write_owner',
            '  exit 0',
            'fi',
            'if [ -d "$lock" ]; then',
            '  now="$(date +%s)"',
            '  mtime="$(stat -c %Y "$lock" 2>/dev/null || echo 0)"',
            '  age="$((now - mtime))"',
            '  if [ "$age" -ge "$stale_seconds" ]; then',
            '    rm -rf "$lock"',
            '    if mkdir "$lock" 2>/dev/null; then',
            '      write_owner',
            '      exit 0',
            '    fi',
            '  fi',
            'fi',
            'echo "lock exists: $lock" >&2',
            'if [ -f "$lock/owner" ]; then cat "$lock/owner" >&2 || true; fi',
            'exit 1',
        ]
    )
    result = ssh_exec(container, script, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'could not acquire container lock {lock_path}: {result.stderr or result.stdout}')


def release_container_lock(container: SshEndpoint, lock_path: str, dry_run: bool) -> None:
    if dry_run:
        return
    ssh_exec(container, f'rm -rf {quoted(lock_path)} >/dev/null 2>&1 || true', check=False)


def upload_manifest(container: SshEndpoint, manifest_path: str, manifest: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    ssh_stream_to_file(container, manifest_path, json_dump(manifest) + '\n')


def container_repo_path(runtime_root: str, record: SnapshotRecord) -> str:
    if record.relpath in ('', '.'):
        return runtime_root
    return str(Path(runtime_root) / record.relpath)


def first_install_prepare_script(runtime_root: str) -> str:
    lines = ['set -eo pipefail', f'mkdir -p {quoted(runtime_root)}', f'cd {quoted(runtime_root)}']
    lines.extend(remote_runtime_env_exports())
    lines.extend(DEFAULT_ENV_PREAMBLE)
    lines.extend(
        [
            '$PIP uninstall -y vllm vllm-ascend vllm_ascend >/dev/null 2>&1 || true',
            f'rm -rf {quoted(str(Path(runtime_root) / "vllm"))} {quoted(str(Path(runtime_root) / "vllm-ascend"))}',
            f'rm -rf {quoted(str(Path(runtime_root) / ".git/modules/vllm"))} {quoted(str(Path(runtime_root) / ".git/modules/vllm-ascend"))}',
        ]
    )
    return '\n'.join(lines)


def render_git_clean(repo_dir: str, preserve_paths: tuple[str, ...]) -> str:
    parts = ['git', '-C', quoted(repo_dir), 'clean', '-ffd']
    for path in preserve_paths:
        parts.extend(['-e', quoted(path)])
    parts.append('>/dev/null')
    return ' '.join(parts)


def materialize_runtime(
    *,
    container: SshEndpoint,
    runtime_root: str,
    container_cache_root: str,
    workspace_id: str,
    marker_dirname: str,
    root_preserve_paths: tuple[str, ...],
    records: list[SnapshotRecord],
    dry_run: bool,
) -> None:
    record_by_relpath = {record.relpath: record for record in records}
    root_record = record_by_relpath['.']
    parity_tracking_ref = f'refs/remotes/parity/{PARITY_BRANCH_NAME}'

    def render_repo_step(record: SnapshotRecord) -> str:
        repo_dir = container_repo_path(runtime_root, record)
        mirror_path = mirror_path_for(container_cache_root, workspace_id, record)
        lines = ['set -eo pipefail', f'mkdir -p {quoted(str(Path(repo_dir).parent))}']
        if record.relpath in ('', '.'):
            lines.append(f'if [ ! -e {quoted(str(Path(repo_dir) / ".git"))} ]; then git init {quoted(repo_dir)} >/dev/null; fi')
        else:
            lines.append(
                f'if [ ! -e {quoted(str(Path(repo_dir) / ".git"))} ]; then rm -rf {quoted(repo_dir)} && git clone --no-checkout {quoted(mirror_path)} {quoted(repo_dir)} >/dev/null; fi'
            )
        lines.extend(
            [
                f'git -C {quoted(repo_dir)} remote get-url parity >/dev/null 2>&1 || git -C {quoted(repo_dir)} remote add parity {quoted(mirror_path)}',
                f'git -C {quoted(repo_dir)} remote set-url parity {quoted(mirror_path)}',
                f'git -C {quoted(repo_dir)} fetch --force --no-recurse-submodules parity {quoted(PARITY_BRANCH_NAME + ":" + parity_tracking_ref)} >/dev/null',
                f'git -C {quoted(repo_dir)} checkout -B parity/current {quoted(parity_tracking_ref)} >/dev/null',
                f'git -C {quoted(repo_dir)} reset --hard {quoted(parity_tracking_ref)} >/dev/null',
            ]
        )
        if record.relpath in ('', '.'):
            lines.append(render_git_clean(repo_dir, root_preserve_paths))
        else:
            lines.append(f'git -C {quoted(repo_dir)} clean -ffd >/dev/null')
        for child in record.submodules:
            child_relpath = child['path'] if record.relpath in ('', '.') else f"{record.relpath}/{child['path']}"
            child_record = record_by_relpath[child_relpath]
            child_mirror = mirror_path_for(container_cache_root, workspace_id, child_record)
            submodule_url_key = f"submodule.{child['name']}.url"
            lines.extend(
                [
                    f'git -C {quoted(repo_dir)} config {quoted(submodule_url_key)} {quoted(child_mirror)}',
                    f'git -C {quoted(repo_dir)} submodule sync -- {quoted(child["path"])} >/dev/null || true',
                ]
            )
        return '\n'.join(lines)

    def collect_scripts(record: SnapshotRecord, out: list[str]) -> None:
        emit_progress('materialize-repo', relpath=record.relpath)
        out.append(render_repo_step(record))
        for child in record.submodules:
            child_relpath = child['path'] if record.relpath in ('', '.') else f"{record.relpath}/{child['path']}"
            collect_scripts(record_by_relpath[child_relpath], out)

    if dry_run:
        return
    parts: list[str] = [
        'set -eo pipefail',
        f'mkdir -p {quoted(runtime_root)}',
        f'mkdir -p {quoted(str(Path(runtime_root) / marker_dirname))}',
    ]
    repo_scripts: list[str] = []
    collect_scripts(root_record, repo_scripts)
    parts.extend(repo_scripts)
    ssh_exec(container, '\n'.join(parts))

def reinstall_required_for_repo(record: SnapshotRecord, patterns: tuple[str, ...]) -> bool:
    return any(glob_match_any(path, patterns) for path in record.changed_paths)


def dependency_install_required_for_repo(record: SnapshotRecord) -> bool:
    return any(glob_match_any(path, DEPENDENCY_INSTALL_PATTERNS) for path in record.changed_paths)


def runtime_install_step_script(
    *,
    runtime_root: str,
    marker_dirname: str,
    container_identity: str,
    step: str,
    uninstall_packages: tuple[str, ...] = (),
) -> str:
    lines = ['set -euo pipefail', f'cd {quoted(runtime_root)}']
    lines.extend(remote_runtime_env_exports())
    lines.append(f'export VAWS_RUNTIME_ROOT={quoted(runtime_root)}')
    lines.extend(DEFAULT_ENV_PREAMBLE)
    if step in {'install-vllm', 'install-vllm-ascend', 'install-vllm-ascend-requirements'}:
        lines.extend(
            [
                'emit_progress() {',
                "  python3 - \"$1\" \"$2\" \"$3\" \"${4:-}\" <<'PY' >&2",
                'import json',
                'import sys',
                'payload = {"phase": sys.argv[1], "message": sys.argv[2]}',
                'if len(sys.argv) > 3 and sys.argv[3]:',
                '    try:',
                '        payload["expected_seconds"] = int(sys.argv[3])',
                '    except ValueError:',
                '        pass',
                f'print("{PROGRESS_SENTINEL}" + json.dumps(payload, ensure_ascii=False))',
                'PY',
                '}',
                'run_with_log_progress() {',
                '  phase="$1"',
                '  message="$2"',
                '  expected_seconds="$3"',
                '  log_file="$4"',
                '  shift 4',
                '  "$@" >"$log_file" 2>&1 &',
                '  pid=$!',
                '  start_ts=$(date +%s)',
                '  while kill -0 "$pid" 2>/dev/null; do',
                '    sleep 8',
                '    if ! kill -0 "$pid" 2>/dev/null; then',
                '      break',
                '    fi',
                '    elapsed=$(( $(date +%s) - start_ts ))',
                '    if [ -s "$log_file" ]; then',
                '      last_line="$(tail -n 1 "$log_file" 2>/dev/null | tr -d \"\\r\" | sed \"s/[^[:print:]\\t]//g\" | cut -c1-180)"',
                '      if [ -n "$last_line" ]; then',
                '        emit_progress "$phase" "$message - $last_line" "$expected_seconds"',
                '      else',
                '        emit_progress "$phase" "$message - still working (elapsed ${elapsed}s)" "$expected_seconds"',
                '      fi',
                '    else',
                '      emit_progress "$phase" "$message - still working (elapsed ${elapsed}s)" "$expected_seconds"',
                '    fi',
                '  done',
                '  set +e',
                '  wait "$pid"',
                '  status=$?',
                '  set -e',
                '  if [ "$status" -ne 0 ]; then',
                '    tail -n 160 "$log_file" >&2 || true',
                '  fi',
                '  return "$status"',
                '}',
                'run_with_progress() {',
                '  phase="$1"',
                '  message="$2"',
                '  expected_seconds="$3"',
                '  shift 3',
                '  log_file="$(mktemp -t parity-step.XXXXXX.log)"',
                '  set +e',
                '  run_with_log_progress "$phase" "$message" "$expected_seconds" "$log_file" "$@"',
                '  status=$?',
                '  set -e',
                '  rm -f "$log_file"',
                '  return "$status"',
                '}',
                'configure_pip_index() {',
                '  unset PIP_EXTRA_INDEX_URL',
                '  export PIP_CONFIG_FILE=/dev/null',
                f'  export PIP_INDEX_URL={quoted(PIP_INDEX_URL)}',
                f'  export PIP_TRUSTED_HOST={quoted(PIP_TRUSTED_HOST)}',
                f'  emit_progress "runtime-pip-index" "using pip index {PIP_INDEX_NAME}" 30',
                '}',
                'pip_install_fast() {',
                '  phase="$1"',
                '  message="$2"',
                '  expected_seconds="$3"',
                '  shift 3',
                '  configure_pip_index',
                f'  run_with_progress "$phase" "$message via {PIP_INDEX_NAME}" "$expected_seconds" "$PYTHON" -m pip "$@"',
                '}',
                'install_editable_fast() {',
                '  phase="$1"',
                '  message="$2"',
                '  target_dir="$3"',
                '  expected_seconds="$4"',
                '  install_cmd="$5"',
                '  log_file="$(mktemp -t parity-install.XXXXXX.log)"',
                '  cd "$target_dir"',
                '  configure_pip_index',
                '  set +e',
                f'  run_with_log_progress "$phase" "$message via {PIP_INDEX_NAME}" "$expected_seconds" "$log_file" bash -lc "$install_cmd"',
                '  status=$?',
                '  set -e',
                '  rm -f "$log_file"',
                '  return "$status"',
                '}',
            ]
        )

    if step == 'uninstall':
        pkg_args = ' '.join(uninstall_packages) if uninstall_packages else 'vllm vllm-ascend vllm_ascend'
        lines.append(f'$PYTHON -m pip uninstall -y {pkg_args} >/dev/null 2>&1 || true')
    elif step == 'install-vllm':
        lines.extend(
            [
                f'cd {quoted(str(Path(runtime_root) / "vllm"))}',
                'export VLLM_TARGET_DEVICE=empty',
                'export TORCH_DEVICE_BACKEND_AUTOLOAD=0',
                'install_editable_fast "runtime-install-vllm" "building editable vllm" . 900 "$PYTHON -m pip install --no-deps -e . --no-build-isolation"',
            ]
        )
    elif step == 'install-vllm-ascend-requirements':
        lines.extend(
            [
                f'cd {quoted(str(Path(runtime_root) / "vllm-ascend"))}',
                'pip_install_fast "runtime-install-vllm-ascend-requirements" "installing vllm-ascend requirements" 900 install -r requirements.txt',
            ]
        )
    elif step == 'install-vllm-ascend':
        lines.extend(
            [
                f'cd {quoted(str(Path(runtime_root) / "vllm-ascend"))}',
                'install_editable_fast "runtime-install-vllm-ascend" "building editable vllm-ascend custom ops" . 2400 "$PYTHON -m pip install --no-deps -v -e . --no-build-isolation"',
            ]
        )
    elif step == 'verify-imports':
        lines.extend(
            [
                "$PYTHON - <<'PY'",
                'import sys',
                'import torch',
                'import torch_npu  # noqa: F401',
                'import vllm',
                'import vllm_ascend',
                'print(f"editable-import-smoke=ok python={sys.executable} torch={torch.__version__} vllm={getattr(vllm, \'__version__\', \'unknown\')}")',
                'PY',
            ]
        )
    elif step == 'verify-deps':
        # Check vllm-ascend deps only.  vllm-ascend intentionally overrides
        # some vllm constraints (e.g. opencv-python-headless) to keep numpy
        # compatible with CANN, so checking vllm deps would false-positive.
        lines.extend(
            [
                "$PYTHON - <<'PY'",
                'import sys',
                'from importlib.metadata import requires, version as pkg_version',
                'from packaging.requirements import Requirement',
                'from packaging.utils import canonicalize_name',
                'from packaging.version import InvalidVersion, Version',
                '',
                'def importable(module):',
                '    try:',
                '        __import__(module)',
                '        return True',
                '    except Exception:',
                '        return False',
                '',
                'def public_version(value):',
                '    try:',
                '        return Version(value).public',
                '    except InvalidVersion:',
                '        return value.split("+", 1)[0]',
                '',
                'def requirement_satisfied(req, installed):',
                '    if not req.specifier:',
                '        return True',
                '    if req.specifier.contains(installed, prereleases=True):',
                '        return True',
                '    public = public_version(installed)',
                '    if public != installed and req.specifier.contains(public, prereleases=True):',
                '        return True',
                '    # Paired vLLM Ascend images provide torch_npu as runtime state.',
                '    if canonicalize_name(req.name) == "torch-npu" and importable("torch_npu"):',
                '        return True',
                '    return False',
                '',
                'errors = []',
                'try:',
                '    reqs = requires("vllm-ascend") or []',
                'except Exception:',
                '    reqs = []',
                'for raw in reqs:',
                '    try:',
                '        r = Requirement(raw)',
                '        if r.marker and not r.marker.evaluate():',
                '            continue',
                '        try:',
                '            installed = pkg_version(r.name)',
                '        except Exception:',
                '            if canonicalize_name(r.name) == "torch-npu" and importable("torch_npu"):',
                '                continue',
                '            raise',
                '        if not requirement_satisfied(r, installed):',
                '            errors.append(f"{r.name}{r.specifier} (installed {installed})")',
                '    except Exception:',
                '        pass',
                'if errors:',
                '    for e in errors:',
                '        print(f"MISMATCH: {e}", file=sys.stderr)',
                '    sys.exit(1)',
                'print("dependency-check=ok")',
                'PY',
            ]
        )
    elif step == 'write-marker':
        lines.extend(
            [
                f'mkdir -p {quoted(str(Path(runtime_root) / marker_dirname))}',
                (
                    'cat > '
                    + quoted(marker_path_for(runtime_root, marker_dirname))
                    + " <<'JSON'\n"
                    + json.dumps(
                        {
                            'container_identity': container_identity,
                            'runtime_root': runtime_root,
                            'updated_at': now_utc(),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + '\nJSON'
                ),
            ]
        )
    else:
        raise ValueError(f'unknown runtime install step: {step}')
    return '\n'.join(lines)


def run_runtime_install_step(
    *,
    container: SshEndpoint,
    runtime_root: str,
    marker_dirname: str,
    container_identity: str,
    step: str,
    stream_progress: bool = False,
    uninstall_packages: tuple[str, ...] = (),
) -> None:
    script = runtime_install_step_script(
        runtime_root=runtime_root,
        marker_dirname=marker_dirname,
        container_identity=container_identity,
        step=step,
        uninstall_packages=uninstall_packages,
    )
    if stream_progress:
        ssh_exec_stream(container, script, stream_progress=True)
    else:
        ssh_exec(container, script)


def read_runtime_install_marker(
    *,
    container: SshEndpoint,
    runtime_root: str,
    marker_dirname: str,
    dry_run: bool,
) -> RuntimeInstallMarker:
    path = marker_path_for(runtime_root, marker_dirname)
    script = '\n'.join(
        [
            'set -eo pipefail',
            f'if [ -f {quoted(path)} ]; then cat {quoted(path)}; fi',
        ]
    )
    result = ssh_exec(container, script)
    content = result.stdout.strip()
    if not content:
        return RuntimeInstallMarker(path=path, record=None)
    try:
        return RuntimeInstallMarker(path=path, record=json.loads(content))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'container runtime-install marker at {path} is invalid JSON: {exc}') from exc


def first_install_needed(marker: RuntimeInstallMarker, container_identity: str, runtime_root: str) -> bool:
    if marker.record is None:
        return True
    return marker.record.get('container_identity') != container_identity or marker.record.get('runtime_root') != runtime_root


def verify_runtime_commits(
    *,
    container: SshEndpoint,
    runtime_root: str,
    records: list[SnapshotRecord],
    dry_run: bool,
) -> dict[str, str]:
    expected = {record.relpath: record.commit for record in records}
    if dry_run:
        return expected
    lines = ['set -eo pipefail']
    for relpath in expected:
        repo_dir = runtime_root if relpath in ('', '.') else str(Path(runtime_root) / relpath)
        lines.append(f"printf '%s %s\\n' {quoted(relpath)} \"$(git -C {quoted(repo_dir)} rev-parse HEAD)\"")
    result = ssh_exec(container, '\n'.join(lines))
    observed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        relpath, commit = line.split(maxsplit=1)
        observed[relpath] = commit
    return observed


def read_runtime_install_env(
    *,
    container: SshEndpoint,
    runtime_root: str,
    dry_run: bool,
) -> dict[str, str]:
    if dry_run:
        return {}
    lines = ['set -euo pipefail', f'mkdir -p {quoted(runtime_root)}', f'cd {quoted(runtime_root)}']
    lines.extend(remote_runtime_env_exports())
    lines.append(f'export VAWS_RUNTIME_ROOT={quoted(runtime_root)}')
    lines.extend(DEFAULT_ENV_PREAMBLE)
    lines.extend(
        [
            "$PYTHON - <<'PY'",
            'import json',
            'import os',
            f'keys = {json.dumps(RUNTIME_INSTALL_ENV_KEYS)}',
            'env = {key: os.environ[key] for key in keys if key in os.environ}',
            'print(json.dumps(env, sort_keys=True))',
            'PY',
        ]
    )
    result = ssh_exec(container, '\n'.join(lines))
    raw = json.loads(result.stdout.strip() or '{}')
    return redact_runtime_env({str(key): str(value) for key, value in raw.items()})


def update_runtime_state(
    *,
    repo_root: Path,
    server_name: str,
    container_identity: str,
    runtime_root: str,
    container_cache_root: str,
    marker_dirname: str,
    records: list[SnapshotRecord],
    first_reinstall_completed: bool,
    runtime_install_env: dict[str, str] | None,
) -> None:
    def apply_update(state: dict[str, Any]) -> None:
        server_state = state.setdefault('servers', {}).setdefault(server_name, {})
        containers = server_state.setdefault('containers', {})
        containers[container_identity] = {
            'runtime_root': runtime_root,
            'container_cache_root': container_cache_root,
            'marker_dirname': marker_dirname,
            'last_sync_at': now_utc(),
            'first_reinstall_completed': first_reinstall_completed,
            'last_snapshot_commits': {record.relpath: record.commit for record in records},
            'last_head_commits': {record.relpath: record.source_head for record in records},
            'last_runtime_install_env': runtime_install_env or {},
        }

    update_state(repo_root, STATE_FILENAME, {'schema_version': 2, 'servers': {}}, apply_update)


def make_manifest(
    *,
    workspace_root: Path,
    workspace_id: str,
    snapshot_id: str,
    server_name: str,
    container_identity: str,
    runtime_root: str,
    container_cache_root: str,
    marker_dirname: str,
    root_preserve_paths: tuple[str, ...],
    records: list[SnapshotRecord],
    runtime_install_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    git_name, git_email = ensure_local_git_identity(workspace_root)
    return {
        'schema_version': 2,
        'generated_at': now_utc(),
        'workspace_root': str(workspace_root),
        'workspace_id': workspace_id,
        'snapshot_id': snapshot_id,
        'server_name': server_name,
        'container_identity': container_identity,
        'runtime_root': runtime_root,
        'container_cache_root': container_cache_root,
        'marker_dirname': marker_dirname,
        'root_preserve_paths': list(root_preserve_paths),
        'git_identity': {'name': git_name, 'email': git_email},
        'repos': [asdict(record) for record in records],
        'runtime_install_env': runtime_install_env or {},
        'local_source_of_truth': 'tracked + staged + unstaged + untracked-nonignored',
    }


def summary_payload(
    *,
    status: str,
    server_name: str,
    container_identity: str,
    workspace_id: str,
    container_cache_root: str | None,
    records: list[SnapshotRecord],
    reinstall_status: str,
    reason: str | None,
    first_install: bool,
    runtime_install_env: dict[str, str] | None = None,
    observed_runtime_commits: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        'status': status,
        'server_name': server_name,
        'container_identity': container_identity,
        'workspace_id': workspace_id,
        'container_cache_root': container_cache_root,
        'first_install': first_install,
        'snapshot_commits': {record.relpath: record.commit for record in records},
        'runtime_commits': observed_runtime_commits,
        'reinstall': reinstall_status,
        'runtime_install_env': runtime_install_env or {},
        'reason': reason,
    }


def build_snapshot_records(workspace_root: Path, workspace_id: str, snapshot_id: str, denylist: tuple[str, ...]) -> list[SnapshotRecord]:
    tree = discover_repo_tree(workspace_root, '.', None)
    child_records: dict[str, SnapshotRecord] = {}
    ordered_records: list[SnapshotRecord] = []
    for node in iter_postorder(tree):
        record = build_synthetic_snapshot(
            node,
            workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            denylist=denylist,
            child_commits=child_records,
        )
        child_records[node.relpath] = record
        ordered_records.append(record)
    return ordered_records


def final_manifest(manifest: dict[str, Any], *, status: str, reinstall_status: str, runtime_commits: dict[str, str] | None) -> dict[str, Any]:
    enriched = dict(manifest)
    enriched['completed_at'] = now_utc()
    enriched['status'] = status
    enriched['reinstall'] = reinstall_status
    enriched['runtime_commits'] = runtime_commits
    return enriched


def run_plan(args: argparse.Namespace) -> int:
    workspace_root = repo_root_from(Path(args.workspace_root))
    workspace_id = normalize_workspace_id(args.workspace_id)
    runtime_root = validate_absolute_posix_path(args.runtime_root, label='runtime root')
    container_cache_root = validate_absolute_posix_path(args.container_cache_root, label='container cache root')
    marker_dirname = validate_relative_posix_path(args.marker_dirname, label='marker dirname')
    root_preserve_paths = resolved_root_preserve_paths(marker_dirname, args.preserve_path)
    snapshot_id = args.snapshot_id or now_utc().replace(':', '').replace('-', '')
    records = build_snapshot_records(workspace_root, workspace_id, snapshot_id, tuple(DEFAULT_DENYLIST))
    try:
        manifest = make_manifest(
            workspace_root=workspace_root,
            workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            server_name=args.server_name,
            container_identity=args.container_identity,
            runtime_root=runtime_root,
            container_cache_root=container_cache_root,
            marker_dirname=marker_dirname,
            root_preserve_paths=root_preserve_paths,
            records=records,
        )
        print(json_dump(manifest))
        return 0
    finally:
        cleanup_synthetic_refs(workspace_root, records)


def run_sync(args: argparse.Namespace) -> int:
    workspace_root = repo_root_from(Path(args.workspace_root))
    workspace_id = normalize_workspace_id(args.workspace_id)
    runtime_root = validate_absolute_posix_path(args.runtime_root, label='runtime root')
    container_cache_root = validate_absolute_posix_path(args.container_cache_root, label='container cache root')
    marker_dirname = validate_relative_posix_path(args.marker_dirname, label='marker dirname')
    root_preserve_paths = resolved_root_preserve_paths(marker_dirname, args.preserve_path)
    snapshot_id = args.snapshot_id or now_utc().replace(':', '').replace('-', '') + '-' + uuid.uuid4().hex[:8]
    container = SshEndpoint(host=args.container_host, port=args.container_port, user=args.container_user)

    emit_progress('snapshot-build', workspace_id=workspace_id, snapshot_id=snapshot_id)
    records = build_snapshot_records(workspace_root, workspace_id, snapshot_id, tuple(DEFAULT_DENYLIST))
    manifest_path = manifest_path_for(container_cache_root, workspace_id, snapshot_id)
    current_phase = 'snapshot-built'
    try:
        try:
            record_map = {record.relpath: record for record in records}
            reinstall_vllm = reinstall_required_for_repo(record_map['vllm'], VLLM_REINSTALL_PATTERNS) if 'vllm' in record_map else False
            reinstall_vllm_ascend = reinstall_required_for_repo(record_map['vllm-ascend'], VLLM_ASCEND_REINSTALL_PATTERNS) if 'vllm-ascend' in record_map else False
            vllm_dependency_changed = dependency_install_required_for_repo(record_map['vllm']) if 'vllm' in record_map else False
            vllm_ascend_dependency_changed = dependency_install_required_for_repo(record_map['vllm-ascend']) if 'vllm-ascend' in record_map else False
            vllm_head_drift = False
            vllm_ascend_head_drift = False

            prior_runtime_state = load_runtime_state(workspace_root)
            last_container_state = (
                prior_runtime_state
                .get('servers', {})
                .get(args.server_name, {})
                .get('containers', {})
                .get(args.container_identity, {})
            )
            last_commits = last_container_state.get('last_snapshot_commits', {})
            last_head_commits = last_container_state.get('last_head_commits', {})
            if 'vllm' in record_map and last_head_commits.get('vllm') and record_map['vllm'].source_head != last_head_commits['vllm']:
                vllm_head_drift = True
                reinstall_vllm = True
            if 'vllm-ascend' in record_map and last_head_commits.get('vllm-ascend') and record_map['vllm-ascend'].source_head != last_head_commits['vllm-ascend']:
                vllm_ascend_head_drift = True
                reinstall_vllm_ascend = True
            if reinstall_vllm and 'vllm-ascend' in record_map:
                reinstall_vllm_ascend = True

            if args.force_reinstall:
                if 'vllm' in record_map:
                    reinstall_vllm = True
                if 'vllm-ascend' in record_map:
                    reinstall_vllm_ascend = True
            install_vllm_ascend_deps = vllm_ascend_dependency_changed or vllm_ascend_head_drift

            snapshot_commits = {record.relpath: record.commit for record in records}
            if args.apply_mode in {'source-only', 'materialize'}:
                runtime_install_env: dict[str, str] = {}
                manifest = make_manifest(
                    workspace_root=workspace_root,
                    workspace_id=workspace_id,
                    snapshot_id=snapshot_id,
                    server_name=args.server_name,
                    container_identity=args.container_identity,
                    runtime_root=runtime_root,
                    container_cache_root=container_cache_root,
                    marker_dirname=marker_dirname,
                    root_preserve_paths=root_preserve_paths,
                    records=records,
                    runtime_install_env=runtime_install_env,
                )
                manifest['apply_mode'] = args.apply_mode
                if args.print_manifest:
                    print(json_dump(manifest))

                if args.dry_run:
                    summary = summary_payload(
                        status='dry-run',
                        server_name=args.server_name,
                        container_identity=args.container_identity,
                        workspace_id=workspace_id,
                        container_cache_root=container_cache_root,
                        records=records,
                        reinstall_status='skipped-by-apply-mode',
                        reason=f'apply_mode={args.apply_mode} skips runtime install/rebuild',
                        first_install=False,
                        runtime_install_env=runtime_install_env,
                        observed_runtime_commits=None,
                    )
                    summary['apply_mode'] = args.apply_mode
                    summary['manifest_path'] = manifest_path
                    print(json_dump(summary))
                    return 0

                lock_path = lock_path_for(container_cache_root, workspace_id, args.container_identity)
                current_phase = 'acquire-lock'
                emit_progress(current_phase, lock_path=lock_path, apply_mode=args.apply_mode)
                acquire_container_lock(container, lock_path, args.dry_run)
                try:
                    current_phase = 'push-mirrors'
                    emit_progress(current_phase, repo_count=len(records), apply_mode=args.apply_mode)
                    all_mirror_paths = [mirror_path_for(container_cache_root, workspace_id, r) for r in records]
                    ensure_remote_bare_repos(container, all_mirror_paths, args.dry_run)
                    for record in records:
                        emit_progress('push-mirror', relpath=record.relpath)
                        push_snapshot_to_mirror(
                            repo=workspace_root if record.relpath in ('', '.') else workspace_root / record.relpath,
                            container=container,
                            mirror_path=mirror_path_for(container_cache_root, workspace_id, record),
                            container_cache_root=container_cache_root,
                            record=record,
                            workspace_id=workspace_id,
                            dry_run=args.dry_run,
                        )

                    current_phase = 'upload-manifest'
                    emit_progress(current_phase, manifest_path=manifest_path, apply_mode=args.apply_mode)
                    upload_manifest(container, manifest_path, manifest, args.dry_run)

                    observed_runtime_commits = None
                    status = 'source-only'
                    if args.apply_mode == 'materialize':
                        current_phase = 'materialize-runtime'
                        emit_progress(current_phase, runtime_root=runtime_root, install='skipped')
                        materialize_runtime(
                            container=container,
                            runtime_root=runtime_root,
                            container_cache_root=container_cache_root,
                            workspace_id=workspace_id,
                            marker_dirname=marker_dirname,
                            root_preserve_paths=root_preserve_paths,
                            records=records,
                            dry_run=args.dry_run,
                        )
                        current_phase = 'verify-runtime-commits'
                        emit_progress(current_phase, repo_count=len(records))
                        observed_runtime_commits = verify_runtime_commits(
                            container=container,
                            runtime_root=runtime_root,
                            records=records,
                            dry_run=args.dry_run,
                        )
                        expected_runtime_commits = {record.relpath: record.commit for record in records}
                        if observed_runtime_commits != expected_runtime_commits:
                            upload_manifest(
                                container,
                                manifest_path,
                                final_manifest(
                                    manifest,
                                    status='failed',
                                    reinstall_status='skipped-by-apply-mode',
                                    runtime_commits=observed_runtime_commits,
                                ),
                                False,
                            )
                            summary = summary_payload(
                                status='failed',
                                server_name=args.server_name,
                                container_identity=args.container_identity,
                                workspace_id=workspace_id,
                                container_cache_root=container_cache_root,
                                records=records,
                                reinstall_status='skipped-by-apply-mode',
                                reason='runtime commit verification mismatch',
                                first_install=False,
                                runtime_install_env=runtime_install_env,
                                observed_runtime_commits=observed_runtime_commits,
                            )
                            summary['apply_mode'] = args.apply_mode
                            summary['manifest_path'] = manifest_path
                            print(json_dump(summary))
                            return 1
                        status = 'materialized'

                    current_phase = 'finalize-manifest'
                    emit_progress(current_phase, manifest_path=manifest_path, apply_mode=args.apply_mode)
                    upload_manifest(
                        container,
                        manifest_path,
                        final_manifest(
                            manifest,
                            status=status,
                            reinstall_status='skipped-by-apply-mode',
                            runtime_commits=observed_runtime_commits,
                        ),
                        False,
                    )
                    emit_progress('complete', status=status, apply_mode=args.apply_mode)
                    summary = summary_payload(
                        status=status,
                        server_name=args.server_name,
                        container_identity=args.container_identity,
                        workspace_id=workspace_id,
                        container_cache_root=container_cache_root,
                        records=records,
                        reinstall_status='skipped-by-apply-mode',
                        reason=f'apply_mode={args.apply_mode} skipped runtime install/rebuild',
                        first_install=False,
                        runtime_install_env=runtime_install_env,
                        observed_runtime_commits=observed_runtime_commits,
                    )
                    summary['apply_mode'] = args.apply_mode
                    summary['manifest_path'] = manifest_path
                    print(json_dump(summary))
                    return 0
                finally:
                    emit_progress('release-lock', lock_path=lock_path)
                    release_container_lock(container, lock_path, args.dry_run)

            if (
                not args.dry_run
                and not reinstall_vllm
                and not reinstall_vllm_ascend
                and last_commits
                and snapshot_commits == last_commits
            ):
                current_phase = 'fast-path-verify'
                emit_progress(current_phase, snapshot_commits=snapshot_commits)
                observed = verify_runtime_commits(
                    container=container,
                    runtime_root=runtime_root,
                    records=records,
                    dry_run=False,
                )
                if observed == snapshot_commits:
                    emit_progress('complete', status='ready', fast_path=True)
                    summary = summary_payload(
                        status='ready',
                        server_name=args.server_name,
                        container_identity=args.container_identity,
                        workspace_id=workspace_id,
                        container_cache_root=container_cache_root,
                        records=records,
                        reinstall_status='not-needed',
                        reason=None,
                        first_install=False,
                        runtime_install_env=last_container_state.get('last_runtime_install_env', {}),
                        observed_runtime_commits=observed,
                    )
                    print(json_dump(summary))
                    return 0

            current_phase = 'read-runtime-marker'
            emit_progress(current_phase, runtime_root=runtime_root)
            marker = read_runtime_install_marker(
                container=container,
                runtime_root=runtime_root,
                marker_dirname=marker_dirname,
                dry_run=args.dry_run,
            )
            first_install = first_install_needed(marker, args.container_identity, runtime_root)
            if first_install:
                current_phase = 'check-consent'
                emit_progress(current_phase, container_identity=args.container_identity)
                consent = resolve_install_consent(workspace_root, args.server_name, args.container_identity)
                if consent != 'allow':
                    summary = summary_payload(
                        status='blocked',
                        server_name=args.server_name,
                        container_identity=args.container_identity,
                        workspace_id=workspace_id,
                        container_cache_root=container_cache_root,
                        records=records,
                        reinstall_status='blocked-by-consent',
                        reason='first-time runtime replacement requires explicit consent',
                        first_install=True,
                        observed_runtime_commits=None,
                    )
                    print(json_dump(summary))
                    return 2
                reinstall_vllm = True if 'vllm' in record_map else reinstall_vllm
                reinstall_vllm_ascend = True if 'vllm-ascend' in record_map else reinstall_vllm_ascend
                install_vllm_ascend_deps = True if 'vllm-ascend' in record_map else install_vllm_ascend_deps

            runtime_install_env: dict[str, str] = {}
            if not args.dry_run:
                current_phase = 'runtime-install-env'
                emit_progress(current_phase, runtime_root=runtime_root)
                runtime_install_env = read_runtime_install_env(
                    container=container,
                    runtime_root=runtime_root,
                    dry_run=args.dry_run,
                )

            manifest = make_manifest(
                workspace_root=workspace_root,
                workspace_id=workspace_id,
                snapshot_id=snapshot_id,
                server_name=args.server_name,
                container_identity=args.container_identity,
                runtime_root=runtime_root,
                container_cache_root=container_cache_root,
                marker_dirname=marker_dirname,
                root_preserve_paths=root_preserve_paths,
                records=records,
                runtime_install_env=runtime_install_env,
            )
            if args.print_manifest:
                print(json_dump(manifest))

            if args.dry_run:
                reinstall_status = 'would-perform' if (reinstall_vllm or reinstall_vllm_ascend) else 'not-needed'
                summary = summary_payload(
                    status='dry-run',
                    server_name=args.server_name,
                    container_identity=args.container_identity,
                    workspace_id=workspace_id,
                    container_cache_root=container_cache_root,
                    records=records,
                    reinstall_status=reinstall_status,
                    reason=None,
                    first_install=first_install,
                    runtime_install_env=runtime_install_env,
                    observed_runtime_commits=None,
                )
                print(json_dump(summary))
                return 0

            lock_path = lock_path_for(container_cache_root, workspace_id, args.container_identity)
            current_phase = 'acquire-lock'
            emit_progress(current_phase, lock_path=lock_path)
            acquire_container_lock(container, lock_path, args.dry_run)
            try:
                current_phase = 'push-mirrors'
                emit_progress(current_phase, repo_count=len(records))
                all_mirror_paths = [mirror_path_for(container_cache_root, workspace_id, r) for r in records]
                ensure_remote_bare_repos(container, all_mirror_paths, args.dry_run)
                for record in records:
                    emit_progress('push-mirror', relpath=record.relpath)
                    push_snapshot_to_mirror(
                        repo=workspace_root if record.relpath in ('', '.') else workspace_root / record.relpath,
                        container=container,
                        mirror_path=mirror_path_for(container_cache_root, workspace_id, record),
                        container_cache_root=container_cache_root,
                        record=record,
                        workspace_id=workspace_id,
                        dry_run=args.dry_run,
                    )

                current_phase = 'upload-manifest'
                emit_progress(current_phase, manifest_path=manifest_path)
                upload_manifest(container, manifest_path, manifest, args.dry_run)

                if first_install:
                    current_phase = 'first-install-prepare'
                    emit_progress(current_phase, runtime_root=runtime_root)
                    ssh_exec(container, first_install_prepare_script(runtime_root))

                current_phase = 'materialize-runtime'
                emit_progress(current_phase, runtime_root=runtime_root)
                materialize_runtime(
                    container=container,
                    runtime_root=runtime_root,
                    container_cache_root=container_cache_root,
                    workspace_id=workspace_id,
                    marker_dirname=marker_dirname,
                    root_preserve_paths=root_preserve_paths,
                    records=records,
                    dry_run=args.dry_run,
                )

                reinstall_status = 'not-needed'
                if reinstall_vllm or reinstall_vllm_ascend:
                    reinstall_status = 'performed'
                    current_phase = 'runtime-install'
                    emit_progress(
                        current_phase,
                        reinstall_vllm=reinstall_vllm,
                        reinstall_vllm_ascend=reinstall_vllm_ascend,
                    )
                    if not first_install:
                        uninstall_pkgs: list[str] = []
                        if reinstall_vllm:
                            uninstall_pkgs.append('vllm')
                        if reinstall_vllm_ascend:
                            uninstall_pkgs.extend(['vllm-ascend', 'vllm_ascend'])
                        emit_progress('runtime-install-uninstall', packages=uninstall_pkgs)
                        run_runtime_install_step(
                            container=container,
                            runtime_root=runtime_root,
                            marker_dirname=marker_dirname,
                            container_identity=args.container_identity,
                            step='uninstall',
                            stream_progress=False,
                            uninstall_packages=tuple(uninstall_pkgs),
                        )
                    if reinstall_vllm:
                        emit_progress('runtime-install-vllm', package='vllm')
                        run_runtime_install_step(
                            container=container,
                            runtime_root=runtime_root,
                            marker_dirname=marker_dirname,
                            container_identity=args.container_identity,
                            step='install-vllm',
                            stream_progress=True,
                        )
                    if reinstall_vllm_ascend:
                        if install_vllm_ascend_deps:
                            emit_progress('runtime-install-vllm-ascend-requirements', requirements='requirements.txt')
                            run_runtime_install_step(
                                container=container,
                                runtime_root=runtime_root,
                                marker_dirname=marker_dirname,
                                container_identity=args.container_identity,
                                step='install-vllm-ascend-requirements',
                                stream_progress=True,
                            )
                        else:
                            emit_progress(
                                'runtime-install-vllm-ascend-requirements',
                                requirements='skipped-paired-image-deps',
                            )
                        emit_progress('runtime-install-vllm-ascend', package='vllm-ascend')
                        run_runtime_install_step(
                            container=container,
                            runtime_root=runtime_root,
                            marker_dirname=marker_dirname,
                            container_identity=args.container_identity,
                            step='install-vllm-ascend',
                            stream_progress=True,
                        )
                    emit_progress('runtime-install-verify-imports')
                    run_runtime_install_step(
                        container=container,
                        runtime_root=runtime_root,
                        marker_dirname=marker_dirname,
                        container_identity=args.container_identity,
                        step='verify-imports',
                        stream_progress=True,
                    )
                    emit_progress('runtime-install-verify-deps')
                    run_runtime_install_step(
                        container=container,
                        runtime_root=runtime_root,
                        marker_dirname=marker_dirname,
                        container_identity=args.container_identity,
                        step='verify-deps',
                        stream_progress=True,
                    )
                    emit_progress('runtime-install-marker')
                    run_runtime_install_step(
                        container=container,
                        runtime_root=runtime_root,
                        marker_dirname=marker_dirname,
                        container_identity=args.container_identity,
                        step='write-marker',
                        stream_progress=False,
                    )

                current_phase = 'verify-runtime-commits'
                emit_progress(current_phase, repo_count=len(records))
                observed_runtime_commits = verify_runtime_commits(
                    container=container,
                    runtime_root=runtime_root,
                    records=records,
                    dry_run=args.dry_run,
                )
                expected_runtime_commits = {record.relpath: record.commit for record in records}
                if observed_runtime_commits != expected_runtime_commits:
                    upload_manifest(
                        container,
                        manifest_path,
                        final_manifest(
                            manifest,
                            status='failed',
                            reinstall_status=reinstall_status,
                            runtime_commits=observed_runtime_commits,
                        ),
                        False,
                    )
                    summary = summary_payload(
                        status='failed',
                        server_name=args.server_name,
                        container_identity=args.container_identity,
                        workspace_id=workspace_id,
                        container_cache_root=container_cache_root,
                        records=records,
                        reinstall_status=reinstall_status,
                        reason='runtime commit verification mismatch',
                        first_install=first_install,
                        runtime_install_env=runtime_install_env,
                        observed_runtime_commits=observed_runtime_commits,
                    )
                    print(json_dump(summary))
                    return 1

                current_phase = 'update-local-state'
                emit_progress(current_phase, server_name=args.server_name)
                update_runtime_state(
                    repo_root=workspace_root,
                    server_name=args.server_name,
                    container_identity=args.container_identity,
                    runtime_root=runtime_root,
                    container_cache_root=container_cache_root,
                    marker_dirname=marker_dirname,
                    records=records,
                    first_reinstall_completed=first_install
                    or last_container_state.get('first_reinstall_completed', False)
                    or reinstall_status == 'performed',
                    runtime_install_env=runtime_install_env,
                )
                current_phase = 'finalize-manifest'
                emit_progress(current_phase, manifest_path=manifest_path)
                upload_manifest(
                    container,
                    manifest_path,
                    final_manifest(
                        manifest,
                        status='ready',
                        reinstall_status=reinstall_status,
                        runtime_commits=observed_runtime_commits,
                    ),
                    False,
                )
                emit_progress('complete', status='ready')
                summary = summary_payload(
                    status='ready',
                    server_name=args.server_name,
                    container_identity=args.container_identity,
                    workspace_id=workspace_id,
                    container_cache_root=container_cache_root,
                    records=records,
                    reinstall_status=reinstall_status,
                    reason=None,
                    first_install=first_install,
                    runtime_install_env=runtime_install_env,
                    observed_runtime_commits=observed_runtime_commits,
                )
                print(json_dump(summary))
                return 0
            finally:
                emit_progress('release-lock', lock_path=lock_path)
                release_container_lock(container, lock_path, args.dry_run)
        except Exception as exc:
            raise RuntimeError(f'{current_phase}: {exc}') from exc
    finally:
        cleanup_synthetic_refs(workspace_root, records)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Prepare or enforce remote code parity for a ready runtime.', allow_abbrev=False)
    subparsers = parser.add_subparsers(dest='command', required=True)

    def add_shared_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument('--workspace-root', required=True, help='Local workspace root.')
        target.add_argument('--workspace-id', required=True, help='Stable workspace id used for container cache namespacing.')
        target.add_argument('--server-name', required=True)
        target.add_argument('--runtime-root', required=True)
        target.add_argument('--container-identity', required=True)
        target.add_argument('--container-cache-root', default=DEFAULT_CONTAINER_CACHE_ROOT)
        target.add_argument('--marker-dirname', default=DEFAULT_MARKER_DIRNAME)
        target.add_argument('--preserve-path', action='append', default=[])

    plan = subparsers.add_parser('plan', help='Build a synthetic snapshot manifest without remote mutations.')
    add_shared_arguments(plan)
    plan.add_argument('--snapshot-id', default=None)

    sync = subparsers.add_parser('sync', help='Publish container-local mirrors, materialize runtime state, and reinstall when required.')
    add_shared_arguments(sync)
    sync.add_argument('--snapshot-id', default=None)
    sync.add_argument('--container-host', required=True)
    sync.add_argument('--container-port', type=int, required=True)
    sync.add_argument('--container-user', required=True)
    sync.add_argument('--force-reinstall', action='store_true', help='Force reinstall of vllm and vllm-ascend regardless of what changed.')
    sync.add_argument('--dry-run', action='store_true')
    sync.add_argument('--print-manifest', action='store_true')
    sync.add_argument(
        '--apply-mode',
        choices=('source-only', 'materialize', 'install'),
        default='install',
        help='source-only publishes snapshots only; materialize updates runtime sources without install/rebuild; install keeps full parity behavior.',
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            return run_plan(args)
        if args.command == 'sync':
            return run_sync(args)
        parser.error(f'unsupported command: {args.command}')
        return 2
    except Exception as exc:
        payload: dict[str, Any] = {
            'status': 'failed',
            'reason': str(exc),
        }
        for field in ('server_name', 'container_identity', 'workspace_id'):
            if hasattr(args, field):
                payload[field] = getattr(args, field)
        print(json_dump(payload))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
