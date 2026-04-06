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

from common import (
    DEFAULT_DENYLIST,
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
    ssh_stream_to_file,
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

DEFAULT_ENV_PREAMBLE = (
    'export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"',
    'export LD_LIBRARY_PATH="/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH:-}"',
    'if [ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]; then source /usr/local/Ascend/ascend-toolkit/set_env.sh; fi',
    'if [ -f /usr/local/Ascend/nnal/atb/set_env.sh ]; then source /usr/local/Ascend/nnal/atb/set_env.sh; fi',
    'if [ -f /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash ]; then source /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash; fi',
    'PYTHON_CANDIDATE="$(ls -1d /usr/local/python*/bin/python3 2>/dev/null | sort -V | tail -n 1 || true)"',
    'if [ -n "$PYTHON_CANDIDATE" ]; then export PYTHON="$PYTHON_CANDIDATE"; elif command -v python3 >/dev/null 2>&1; then export PYTHON="$(command -v python3)"; elif command -v python >/dev/null 2>&1; then export PYTHON="$(command -v python)"; else echo "python not found" >&2; exit 127; fi',
    'export PIP="$PYTHON -m pip"',
    'export VLLM_WORKER_MULTIPROC_METHOD=spawn',
    'export OMP_NUM_THREADS=1',
    'export MKL_NUM_THREADS=1',
)

DEFAULT_CONTAINER_CACHE_ROOT = '/root/.cache/vaws/remote-code-parity'
DEFAULT_MARKER_DIRNAME = '.remote-code-parity'
DEFAULT_ROOT_PRESERVE_PATHS = ('Mooncake',)
STATE_FILENAME = 'runtime-state.json'
CONSENT_FILENAME = 'install-consents.json'
WORKSPACE_ID_PATTERN = re.compile(r'[^A-Za-z0-9._-]+')
PROGRESS_SENTINEL = '__VAWS_PARITY_PROGRESS__='
PARITY_BRANCH_NAME = 'parity-current'


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


def commit_message(workspace_id: str, snapshot_id: str, relpath: str) -> str:
    return f'remote-code-parity snapshot {workspace_id} {snapshot_id} {sanitize_repo_id(relpath)}'


def build_synthetic_snapshot(
    node: RepoNode,
    *,
    workspace_id: str,
    snapshot_id: str,
    denylist: tuple[str, ...],
    child_commits: dict[str, SnapshotRecord],
) -> SnapshotRecord:
    repo = node.repo_path
    parent = git_head(repo)
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
        if parent:
            git(repo, ['read-tree', parent], env=env)
        git(repo, ['add', '-A'], env=env)
        reset_specs = reset_pathspecs(node, denylist)
        if reset_specs:
            git(repo, ['reset', '-q', '--', *reset_specs], env=env)

        submodule_records: list[dict[str, str]] = []
        for child in node.children:
            child_record = child_commits[child.relpath]
            child_rel_to_repo = child.repo_path.relative_to(repo).as_posix()
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
        commit_args = ['commit-tree', tree]
        if parent:
            commit_args.extend(['-p', parent])
        commit_args.extend(['-m', commit_message(workspace_id, snapshot_id, node.relpath)])
        commit = git(repo, commit_args, env=env).stdout.strip()
        git(repo, ['update-ref', ref, commit])

        if parent:
            diff = git(repo, ['diff', '--name-only', f'{parent}..{commit}']).stdout.splitlines()
        else:
            diff = git(repo, ['show', '--pretty=', '--name-only', commit]).stdout.splitlines()

        return SnapshotRecord(
            relpath=node.relpath,
            repo_id=sanitize_repo_id(node.relpath),
            parent=parent,
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


def manifest_path_for(container_cache_root: str, workspace_id: str, snapshot_id: str) -> str:
    return str(Path(cache_workspace_root(container_cache_root, workspace_id)) / 'manifests' / f'{snapshot_id}.json')


def lock_path_for(container_cache_root: str, workspace_id: str, container_identity: str) -> str:
    token = re.sub(r'[^A-Za-z0-9._-]+', '-', container_identity).strip('.-') or 'container'
    return str(Path(cache_workspace_root(container_cache_root, workspace_id)) / 'locks' / token)


def marker_path_for(runtime_root: str, marker_dirname: str) -> str:
    return str(Path(runtime_root) / marker_dirname / 'runtime-install.json')


def ensure_remote_bare_repo(container: SshEndpoint, mirror_path: str, dry_run: bool) -> None:
    if dry_run:
        return
    script = '\n'.join(
        [
            'set -eo pipefail',
            f'mkdir -p {quoted(str(Path(mirror_path).parent))}',
            f'if [ -e {quoted(mirror_path)} ] && [ ! -d {quoted(str(Path(mirror_path) / "objects"))} ]; then rm -rf {quoted(mirror_path)}; fi',
            f'if [ ! -d {quoted(mirror_path)} ]; then git init --bare {quoted(mirror_path)} >/dev/null; fi',
        ]
    )
    ssh_exec(container, script)


def push_snapshot_to_mirror(
    repo: Path,
    *,
    container: SshEndpoint,
    mirror_path: str,
    record: SnapshotRecord,
    workspace_id: str,
    dry_run: bool,
) -> None:
    ensure_remote_bare_repo(container, mirror_path, dry_run)
    if dry_run:
        return
    remote_url = f'ssh://{container.user}@{container.host}:{container.port}{mirror_path}'
    target_ref = f'refs/parity/{workspace_id}/current'
    git(
        repo,
        [
            'push',
            '--force',
            remote_url,
            f'{record.commit}:{target_ref}',
            f'{record.commit}:refs/heads/{PARITY_BRANCH_NAME}',
        ],
    )

def acquire_container_lock(container: SshEndpoint, lock_path: str, dry_run: bool) -> None:
    if dry_run:
        return
    script = '\n'.join(
        [
            'set -eo pipefail',
            f'mkdir -p {quoted(str(Path(lock_path).parent))}',
            f'mkdir {quoted(lock_path)}',
        ]
    )
    result = ssh_exec(container, script, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'could not acquire container lock {lock_path}: {result.stderr or result.stdout}')


def release_container_lock(container: SshEndpoint, lock_path: str, dry_run: bool) -> None:
    if dry_run:
        return
    ssh_exec(container, f'rmdir {quoted(lock_path)} >/dev/null 2>&1 || true', check=False)


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

    def walk(record: SnapshotRecord) -> None:
        emit_progress('materialize-repo', relpath=record.relpath)
        if not dry_run:
            ssh_exec(container, render_repo_step(record))
        for child in record.submodules:
            child_relpath = child['path'] if record.relpath in ('', '.') else f"{record.relpath}/{child['path']}"
            walk(record_by_relpath[child_relpath])

    if dry_run:
        return
    ssh_exec(
        container,
        '\n'.join(
            [
                'set -eo pipefail',
                f'mkdir -p {quoted(runtime_root)}',
                f'mkdir -p {quoted(str(Path(runtime_root) / marker_dirname))}',
            ]
        ),
    )
    walk(root_record)

def reinstall_required_for_repo(record: SnapshotRecord, patterns: tuple[str, ...]) -> bool:
    return any(glob_match_any(path, patterns) for path in record.changed_paths)


def runtime_install_script(
    *,
    runtime_root: str,
    marker_dirname: str,
    reinstall_vllm: bool,
    reinstall_vllm_ascend: bool,
    container_identity: str,
) -> str:
    lines = ['set -eo pipefail', f'cd {quoted(runtime_root)}']
    lines.extend(DEFAULT_ENV_PREAMBLE)
    lines.extend(
        [
            'upgrade_packaging_stack() {',
            '  $PIP install --upgrade "pip>=24.0" "setuptools>=77" "wheel>=0.43" "packaging>=24.0" >/dev/null 2>&1 || true',
            '}',
            'install_with_fallback() {',
            '  target_dir="$1"',
            '  install_cmd="$2"',
            '  log_file="$(mktemp -t parity-install.XXXXXX.log)"',
            '  cd "$target_dir"',
            '  if bash -lc "$install_cmd" >"$log_file" 2>&1; then',
            '    rm -f "$log_file"',
            '    return 0',
            '  fi',
            '  status=$?',
            '  if grep -Eiq "project\\.license|license[^[:alnum:]]|spdx|pyproject" "$log_file"; then',
            '    upgrade_packaging_stack',
            '    if bash -lc "$install_cmd" >>"$log_file" 2>&1; then',
            '      rm -f "$log_file"',
            '      return 0',
            '    fi',
            '    status=$?',
            '    alt_cmd="$(printf "%s" "$install_cmd" | sed "s/--no-build-isolation//g")"',
            '    if bash -lc "$alt_cmd" >>"$log_file" 2>&1; then',
            '      rm -f "$log_file"',
            '      return 0',
            '    fi',
            '    status=$?',
            '  fi',
            '  tail -n 120 "$log_file" >&2 || true',
            '  rm -f "$log_file"',
            '  return "$status"',
            '}',
        ]
    )
    if reinstall_vllm or reinstall_vllm_ascend:
        lines.append('$PIP uninstall -y vllm vllm-ascend vllm_ascend >/dev/null 2>&1 || true')
    if reinstall_vllm:
        lines.extend(
            [
                f'cd {quoted(str(Path(runtime_root) / "vllm"))}',
                'export VLLM_TARGET_DEVICE=empty',
                'install_with_fallback . "$PIP install -e . --no-build-isolation"',
            ]
        )
    if reinstall_vllm_ascend:
        lines.extend(
            [
                f'cd {quoted(str(Path(runtime_root) / "vllm-ascend"))}',
                '$PIP install -r requirements.txt >/dev/null',
                'install_with_fallback . "$PIP install -v -e . --no-build-isolation"',
            ]
        )
    lines.extend(
        [
            '$PYTHON - <<\'PY\'',
            'import importlib.util',
            'import torch_npu  # noqa: F401',
            "assert importlib.util.find_spec('vllm') is not None",
            "assert importlib.util.find_spec('vllm_ascend') is not None",
            "print('editable-import-smoke=ok')",
            'PY',
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
                        'reinstall_vllm': reinstall_vllm,
                        'reinstall_vllm_ascend': reinstall_vllm_ascend,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + '\nJSON'
            ),
        ]
    )
    return '\n'.join(lines)

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
) -> None:
    state = load_runtime_state(repo_root)
    server_state = state.setdefault('servers', {}).setdefault(server_name, {})
    containers = server_state.setdefault('containers', {})
    containers[container_identity] = {
        'runtime_root': runtime_root,
        'container_cache_root': container_cache_root,
        'marker_dirname': marker_dirname,
        'last_sync_at': now_utc(),
        'first_reinstall_completed': first_reinstall_completed,
        'last_snapshot_commits': {record.relpath: record.commit for record in records},
    }
    save_runtime_state(repo_root, state)


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
                for record in records:
                    emit_progress('push-mirror', relpath=record.relpath)
                    push_snapshot_to_mirror(
                        repo=workspace_root if record.relpath in ('', '.') else workspace_root / record.relpath,
                        container=container,
                        mirror_path=mirror_path_for(container_cache_root, workspace_id, record),
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
                    ssh_exec(
                        container,
                        runtime_install_script(
                            runtime_root=runtime_root,
                            marker_dirname=marker_dirname,
                            reinstall_vllm=reinstall_vllm,
                            reinstall_vllm_ascend=reinstall_vllm_ascend,
                            container_identity=args.container_identity,
                        ),
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
                    or load_runtime_state(workspace_root).get('servers', {}).get(args.server_name, {}).get('containers', {}).get(args.container_identity, {}).get('first_reinstall_completed', False)
                    or reinstall_status == 'performed',
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
    parser = argparse.ArgumentParser(description='Prepare or enforce remote code parity for a ready runtime.')
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
    sync.add_argument('--dry-run', action='store_true')
    sync.add_argument('--print-manifest', action='store_true')

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
