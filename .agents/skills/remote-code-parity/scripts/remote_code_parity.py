#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from common import (
    DEFAULT_DENYLIST,
    DEFAULT_REJECTED_FS_TYPES,
    FIRST_INSTALL_MIN_FREE_BYTES,
    HARD_MIN_FREE_BYTES,
    SshEndpoint,
    ensure_local_git_identity,
    git,
    glob_match_any,
    human_bytes,
    json_dump,
    load_state,
    now_utc,
    quoted,
    repo_root_from,
    sanitize_repo_id,
    save_state,
    ssh_exec,
    ssh_stream_to_file,
    is_git_worktree,
)


VLLM_REINSTALL_PATTERNS = (
    "requirements*",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "CMakeLists.txt",
    "cmake/**",
    "csrc/**",
    "**/*.cu",
    "**/*.cuh",
    "**/*.cpp",
    "**/*.cc",
    "**/*.h",
    "**/*.hpp",
)

VLLM_ASCEND_REINSTALL_PATTERNS = VLLM_REINSTALL_PATTERNS + (
    "vllm_ascend/_cann_ops_custom/**",
)

DEFAULT_ENV_PREAMBLE = (
    "if [ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]; then source /usr/local/Ascend/ascend-toolkit/set_env.sh; fi",
    "if [ -f /usr/local/Ascend/nnal/atb/set_env.sh ]; then source /usr/local/Ascend/nnal/atb/set_env.sh; fi",
    "if [ -f /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash ]; then source /vllm-workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash; fi",
    "export PATH=/usr/local/python3.11.14/bin:$PATH",
    "export PYTHON=/usr/local/python3.11.14/bin/python3",
    "export PIP=/usr/local/python3.11.14/bin/pip",
    "export VLLM_WORKER_MULTIPROC_METHOD=spawn",
    "export OMP_NUM_THREADS=1",
    "export MKL_NUM_THREADS=1",
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
    children: list["RepoNode"] = field(default_factory=list)


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


def ensure_populated_worktree(repo: Path, relpath: str) -> None:
    if not repo.exists():
        raise RuntimeError(
            f"required repo path {relpath} is missing; initialize submodules before remote-code-parity"
        )
    if not is_git_worktree(repo):
        raise RuntimeError(
            f"required repo path {relpath} is not a populated Git worktree; run repo-init or git submodule update --init --recursive before remote-code-parity"
        )


def list_submodules(repo: Path) -> list[SubmoduleEntry]:
    gitmodules = repo / ".gitmodules"
    if not gitmodules.exists():
        return []
    result = git(repo, ["config", "--file", str(gitmodules), "--get-regexp", r"^submodule\..*\.path$"], check=False)
    entries: list[SubmoduleEntry] = []
    if result.returncode != 0 or not result.stdout.strip():
        return entries
    for line in result.stdout.splitlines():
        key, path = line.split(maxsplit=1)
        name = key.removeprefix("submodule.").removesuffix(".path")
        entries.append(SubmoduleEntry(name=name, path=path.strip()))
    return entries


def discover_repo_tree(repo: Path, relpath: str = ".", submodule_name: str | None = None) -> RepoNode:
    ensure_populated_worktree(repo, relpath)
    node = RepoNode(relpath=relpath, repo_path=repo, submodule_name=submodule_name)
    for entry in list_submodules(repo):
        child_repo = repo / entry.path
        child_relpath = entry.path if relpath in ("", ".") else f"{relpath}/{entry.path}"
        ensure_populated_worktree(child_repo, child_relpath)
        node.children.append(discover_repo_tree(child_repo, child_relpath, entry.name))
    return node


def iter_postorder(node: RepoNode):
    for child in node.children:
        yield from iter_postorder(child)
    yield node


def git_head(repo: Path) -> str | None:
    result = git(repo, ["rev-parse", "--verify", "HEAD"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_current_branch(repo: Path) -> str | None:
    result = git(repo, ["symbolic-ref", "--short", "HEAD"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def exclude_pathspecs(node: RepoNode, denylist: tuple[str, ...]) -> list[str]:
    specs: list[str] = []
    for child in node.children:
        specs.append(f":(exclude){child.repo_path.relative_to(node.repo_path).as_posix()}")
    for pattern in denylist:
        if any(ch in pattern for ch in "*?[]"):
            specs.append(f":(glob,exclude){pattern}")
        else:
            specs.append(f":(exclude){pattern}")
    return specs


def synthetic_ref(workspace_id: str, snapshot_id: str, relpath: str) -> str:
    repo_id = sanitize_repo_id(relpath)
    return f"refs/parity/{workspace_id}/{snapshot_id}/{repo_id}"


def commit_message(workspace_id: str, snapshot_id: str, relpath: str) -> str:
    repo_id = sanitize_repo_id(relpath)
    return f"remote-code-parity snapshot {workspace_id} {snapshot_id} {repo_id}"


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
    temp_index = tempfile.NamedTemporaryFile(prefix="parity-index-", delete=False)
    temp_index.close()
    temp_index_path = Path(temp_index.name)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = temp_index.name
    env["GIT_OPTIONAL_LOCKS"] = "0"
    author_name, author_email = ensure_local_git_identity(repo)
    env.setdefault("GIT_AUTHOR_NAME", author_name or "remote-code-parity")
    env.setdefault("GIT_AUTHOR_EMAIL", author_email or "remote-code-parity@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", author_name or "remote-code-parity")
    env.setdefault("GIT_COMMITTER_EMAIL", author_email or "remote-code-parity@example.invalid")

    try:
        if parent:
            git(repo, ["read-tree", parent], env=env)
        add_args = ["add", "-A", "-f", "--", "."]
        add_args.extend(exclude_pathspecs(node, denylist))
        git(repo, add_args, env=env)

        submodule_records: list[dict[str, str]] = []
        for child in node.children:
            child_record = child_commits[child.relpath]
            child_rel_to_repo = child.repo_path.relative_to(repo).as_posix()
            git(
                repo,
                ["update-index", "--add", "--cacheinfo", f"160000,{child_record.commit},{child_rel_to_repo}"],
                env=env,
            )
            submodule_records.append(
                {
                    "name": child.submodule_name or child_rel_to_repo,
                    "path": child_rel_to_repo,
                    "commit": child_record.commit,
                    "repo_id": child_record.repo_id,
                }
            )

        tree = git(repo, ["write-tree"], env=env).stdout.strip()
        commit_args = ["commit-tree", tree]
        if parent:
            commit_args.extend(["-p", parent])
        commit_args.extend(["-m", commit_message(workspace_id, snapshot_id, node.relpath)])
        commit = git(repo, commit_args, env=env).stdout.strip()
        git(repo, ["update-ref", ref, commit])

        if parent:
            diff = git(repo, ["diff", "--name-only", f"{parent}..{commit}"]).stdout.splitlines()
        else:
            diff = git(repo, ["show", "--pretty=", "--name-only", commit]).stdout.splitlines()

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
        repo = workspace_root if record.relpath in ("", ".") else workspace_root / record.relpath
        git(repo, ["update-ref", "-d", record.ref], check=False)


def load_runtime_state(repo_root: Path) -> dict[str, Any]:
    return load_state(repo_root, "runtime-state.json", {"schema_version": 1, "servers": {}})


def save_runtime_state(repo_root: Path, state: dict[str, Any]) -> Path:
    return save_state(repo_root, "runtime-state.json", state)


def choose_storage_root(
    *,
    repo_root: Path,
    server_name: str,
    host: SshEndpoint,
    provided_root: str | None,
    candidate_roots: list[str],
    first_install: bool,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    state = load_runtime_state(repo_root)
    server_state = state.setdefault("servers", {}).setdefault(server_name, {})
    threshold = FIRST_INSTALL_MIN_FREE_BYTES if first_install else HARD_MIN_FREE_BYTES

    attempts: list[dict[str, Any]] = []
    roots_to_try: list[str] = []
    if provided_root:
        roots_to_try.append(provided_root)
    if server_state.get("storage_root") and server_state["storage_root"] not in roots_to_try:
        roots_to_try.append(server_state["storage_root"])
    roots_to_try.extend(root for root in candidate_roots if root not in roots_to_try)

    if not roots_to_try:
        raise RuntimeError("no storage root or storage-root candidates were supplied")

    if dry_run:
        chosen = roots_to_try[0]
        attempts.append({"path": chosen, "status": "dry-run-assumed", "free_bytes": None, "fs_type": None})
        return chosen, {"attempts": attempts, "threshold_bytes": threshold}

    for root in roots_to_try:
        script = (
            f"mkdir -p {quoted(root)} && "
            f"df -PT {quoted(root)} | tail -1 && "
            f"python3 - <<'PY'\n"
            f"import os\n"
            f"st = os.statvfs({root!r})\n"
            f"print(st.f_bavail * st.f_frsize)\n"
            f"PY"
        )
        result = ssh_exec(host, script, check=False)
        if result.returncode != 0:
            attempts.append({"path": root, "status": "probe-failed", "stderr_tail": result.stderr[-400:]})
            continue
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            attempts.append({"path": root, "status": "malformed-probe-output", "stdout": result.stdout})
            continue
        df_line = lines[-2]
        free_line = lines[-1]
        parts = df_line.split()
        fs_type = parts[1].lower() if len(parts) >= 2 else "unknown"
        try:
            free_bytes = int(free_line)
        except ValueError:
            attempts.append({"path": root, "status": "invalid-free-byte-output", "stdout": result.stdout})
            continue

        if fs_type in DEFAULT_REJECTED_FS_TYPES:
            attempts.append({"path": root, "status": "rejected-fs-type", "fs_type": fs_type, "free_bytes": free_bytes})
            continue
        if free_bytes < threshold:
            attempts.append({"path": root, "status": "below-threshold", "fs_type": fs_type, "free_bytes": free_bytes})
            continue

        server_state["storage_root"] = root
        save_runtime_state(repo_root, state)
        attempts.append({"path": root, "status": "selected", "fs_type": fs_type, "free_bytes": free_bytes})
        return root, {"attempts": attempts, "threshold_bytes": threshold}

    raise RuntimeError("could not find a usable storage_root:\n" + json_dump({"attempts": attempts, "threshold_bytes": threshold}))


def load_consent(repo_root: Path) -> dict[str, Any]:
    return load_state(repo_root, "install-consents.json", {"schema_version": 1, "consents": {}})


def resolve_install_consent(repo_root: Path, server_name: str, container_identity: str) -> str:
    state = load_consent(repo_root)
    decision = (
        state.get("consents", {})
        .get(server_name, {})
        .get("containers", {})
        .get(container_identity, {})
        .get("decision")
    )
    return decision or "unknown"


def ensure_remote_bare_repo(host: SshEndpoint, mirror_path: str, dry_run: bool) -> None:
    if dry_run:
        return
    script = (
        f"mkdir -p {quoted(str(Path(mirror_path).parent))} && "
        f"if [ ! -d {quoted(mirror_path)} ]; then git init --bare {quoted(mirror_path)} >/dev/null; fi"
    )
    ssh_exec(host, script)


def push_snapshot_to_mirror(
    repo: Path,
    *,
    host: SshEndpoint,
    mirror_path: str,
    record: SnapshotRecord,
    workspace_id: str,
    dry_run: bool,
) -> None:
    ensure_remote_bare_repo(host, mirror_path, dry_run)
    if dry_run:
        return
    remote_url = f"ssh://{host.user}@{host.host}:{host.port}{mirror_path}"
    target_ref = f"refs/parity/{workspace_id}/current"
    git(repo, ["push", "--force", remote_url, f"{record.commit}:{target_ref}"])


def remote_workspace_root(storage_root: str, workspace_id: str) -> str:
    return f"{storage_root.rstrip('/')}/remote-code-parity/workspaces/{workspace_id}"


def mirror_path_for(storage_root: str, workspace_id: str, record: SnapshotRecord) -> str:
    root = Path(remote_workspace_root(storage_root, workspace_id)) / "mirrors"
    repo_id = record.repo_id
    if repo_id == "workspace":
        return str(root / "workspace.git")
    return str(root / "nested" / f"{repo_id}.git")


def host_manifest_path(storage_root: str, workspace_id: str, snapshot_id: str) -> str:
    root = Path(remote_workspace_root(storage_root, workspace_id)) / "manifests"
    return str(root / f"{snapshot_id}.json")


def host_lock_path(storage_root: str, workspace_id: str, container_identity: str) -> str:
    root = Path(remote_workspace_root(storage_root, workspace_id)) / "locks"
    digest = hashlib.sha256(container_identity.encode("utf-8")).hexdigest()[:16]
    return str(root / f"{digest}.lock")


def acquire_host_lock(host: SshEndpoint, lock_path: str, dry_run: bool) -> None:
    if dry_run:
        return
    script = (
        f"mkdir -p {quoted(str(Path(lock_path).parent))} && "
        f"mkdir {quoted(lock_path)}"
    )
    result = ssh_exec(host, script, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"could not acquire host lock {lock_path}: {result.stderr or result.stdout}")


def release_host_lock(host: SshEndpoint, lock_path: str, dry_run: bool) -> None:
    if dry_run:
        return
    ssh_exec(host, f"rmdir {quoted(lock_path)} >/dev/null 2>&1 || true", check=False)


def upload_manifest(host: SshEndpoint, manifest_path: str, manifest: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    ssh_stream_to_file(host, manifest_path, json_dump(manifest) + "\n")


def container_repo_paths(runtime_root: str, record: SnapshotRecord) -> str:
    relpath = record.relpath
    if relpath in ("", "."):
        return runtime_root
    return str(Path(runtime_root) / relpath)


def materialize_runtime(
    *,
    container: SshEndpoint,
    runtime_root: str,
    container_storage_root: str,
    workspace_id: str,
    records: list[SnapshotRecord],
    dry_run: bool,
) -> None:
    record_by_relpath = {record.relpath: record for record in records}
    root_record = record_by_relpath["."]
    # Top-down materialization so each repo can rewrite its child submodule URLs before update.
    def render_repo_step(record: SnapshotRecord) -> list[str]:
        repo_dir = container_repo_paths(runtime_root, record)
        mirror_path = mirror_path_for(container_storage_root, workspace_id, record)
        lines = [
            f"mkdir -p {quoted(str(Path(repo_dir).parent))}",
            f"if [ ! -d {quoted(repo_dir + '/.git')} ]; then rm -rf {quoted(repo_dir)} && git clone --no-checkout {quoted(mirror_path)} {quoted(repo_dir)} >/dev/null; fi",
            f"git -C {quoted(repo_dir)} remote get-url parity >/dev/null 2>&1 || git -C {quoted(repo_dir)} remote add parity {quoted(mirror_path)}",
            f"git -C {quoted(repo_dir)} remote set-url parity {quoted(mirror_path)}",
            f"git -C {quoted(repo_dir)} fetch --force parity refs/parity/{workspace_id}/current >/dev/null",
            f"git -C {quoted(repo_dir)} checkout -B parity/current FETCH_HEAD >/dev/null",
            f"git -C {quoted(repo_dir)} reset --hard FETCH_HEAD >/dev/null",
            f"git -C {quoted(repo_dir)} clean -ffd >/dev/null",
        ]
        for child in record.submodules:
            child_record = record_by_relpath[child["path"] if record.relpath in ("", ".") else f'{record.relpath}/{child["path"]}']
            child_mirror = mirror_path_for(container_storage_root, workspace_id, child_record)
            lines.extend(
                [
                    f"git -C {quoted(repo_dir)} config submodule.{quoted(child['name'])}.url {quoted(child_mirror)}",
                    f"git -C {quoted(repo_dir)} submodule sync -- {quoted(child['path'])} >/dev/null || true",
                    f"git -C {quoted(repo_dir)} submodule update --init --checkout --force -- {quoted(child['path'])} >/dev/null",
                ]
            )
        return lines

    def walk(record: SnapshotRecord) -> list[str]:
        lines = render_repo_step(record)
        for child in record.submodules:
            child_relpath = child["path"] if record.relpath in ("", ".") else f"{record.relpath}/{child['path']}"
            lines.extend(walk(record_by_relpath[child_relpath]))
        return lines

    script_lines = ["set -eo pipefail", f"mkdir -p {quoted(runtime_root)}"]
    script_lines.extend(walk(root_record))
    script = "\n".join(script_lines)
    if dry_run:
        return
    ssh_exec(container, script)


def reinstall_required_for_repo(record: SnapshotRecord, patterns: tuple[str, ...]) -> bool:
    return any(glob_match_any(path, patterns) for path in record.changed_paths)


def runtime_install_script(
    *,
    runtime_root: str,
    reinstall_vllm: bool,
    reinstall_vllm_ascend: bool,
    container_identity: str,
) -> str:
    lines = ["set -eo pipefail", f"cd {quoted(runtime_root)}"]
    lines.extend(DEFAULT_ENV_PREAMBLE)
    if reinstall_vllm or reinstall_vllm_ascend:
        lines.append("$PIP uninstall -y vllm vllm-ascend vllm_ascend >/dev/null 2>&1 || true")
    if reinstall_vllm:
        lines.extend(
            [
                f"cd {quoted(str(Path(runtime_root) / 'vllm'))}",
                "export VLLM_TARGET_DEVICE=empty",
                "$PIP install -e . --no-build-isolation",
            ]
        )
    if reinstall_vllm_ascend:
        lines.extend(
            [
                f"cd {quoted(str(Path(runtime_root) / 'vllm-ascend'))}",
                "$PIP install -r requirements.txt",
                "$PIP install -v -e . --no-build-isolation",
            ]
        )
    lines.extend(
        [
            "$PYTHON - <<'PY'",
            "import importlib.util",
            "import torch_npu  # noqa: F401",
            "assert importlib.util.find_spec('vllm') is not None",
            "assert importlib.util.find_spec('vllm_ascend') is not None",
            "print('editable-import-smoke=ok')",
            "PY",
            f"mkdir -p {quoted(str(Path(runtime_root) / '.remote-code-parity'))}",
            (
                "cat > "
                + quoted(str(Path(runtime_root) / ".remote-code-parity/runtime-install.json"))
                + " <<'JSON'\n"
                + json.dumps(
                    {
                        "container_identity": container_identity,
                        "updated_at": now_utc(),
                        "reinstall_vllm": reinstall_vllm,
                        "reinstall_vllm_ascend": reinstall_vllm_ascend,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\nJSON"
            ),
        ]
    )
    return "\n".join(lines)


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
    lines = ["set -eo pipefail"]
    for relpath, _commit in expected.items():
        repo_dir = runtime_root if relpath in ("", ".") else str(Path(runtime_root) / relpath)
        lines.append(f"printf '%s %s\\n' {quoted(relpath)} \"$(git -C {quoted(repo_dir)} rev-parse HEAD)\"")
    result = ssh_exec(container, "\n".join(lines))
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
    storage_root: str,
    records: list[SnapshotRecord],
    first_reinstall_completed: bool,
) -> None:
    state = load_runtime_state(repo_root)
    server_state = state.setdefault("servers", {}).setdefault(server_name, {})
    server_state["storage_root"] = storage_root
    containers = server_state.setdefault("containers", {})
    containers[container_identity] = {
        "runtime_root": runtime_root,
        "last_sync_at": now_utc(),
        "first_reinstall_completed": first_reinstall_completed,
        "last_snapshot_commits": {record.relpath: record.commit for record in records},
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
    storage_root: str,
    records: list[SnapshotRecord],
    storage_probe: dict[str, Any],
) -> dict[str, Any]:
    git_name, git_email = ensure_local_git_identity(workspace_root)
    return {
        "schema_version": 1,
        "generated_at": now_utc(),
        "workspace_root": str(workspace_root),
        "workspace_id": workspace_id,
        "snapshot_id": snapshot_id,
        "server_name": server_name,
        "container_identity": container_identity,
        "runtime_root": runtime_root,
        "storage_root": storage_root,
        "git_identity": {"name": git_name, "email": git_email},
        "storage_probe": storage_probe,
        "repos": [asdict(record) for record in records],
    }


def summary_payload(
    *,
    status: str,
    server_name: str,
    container_identity: str,
    workspace_id: str,
    storage_root: str | None,
    records: list[SnapshotRecord],
    reinstall_status: str,
    reason: str | None,
    observed_runtime_commits: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "server_name": server_name,
        "container_identity": container_identity,
        "workspace_id": workspace_id,
        "storage_root": storage_root,
        "snapshot_commits": {record.relpath: record.commit for record in records},
        "runtime_commits": observed_runtime_commits,
        "reinstall": reinstall_status,
        "reason": reason,
    }


def build_snapshot_records(workspace_root: Path, workspace_id: str, snapshot_id: str, denylist: tuple[str, ...]) -> list[SnapshotRecord]:
    tree = discover_repo_tree(workspace_root, ".", None)
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


def run_plan(args: argparse.Namespace) -> int:
    workspace_root = repo_root_from(Path(args.workspace_root))
    snapshot_id = args.snapshot_id or now_utc().replace(":", "").replace("-", "")
    records = build_snapshot_records(workspace_root, args.workspace_id, snapshot_id, tuple(DEFAULT_DENYLIST))
    try:
        manifest = make_manifest(
            workspace_root=workspace_root,
            workspace_id=args.workspace_id,
            snapshot_id=snapshot_id,
            server_name=args.server_name,
            container_identity=args.container_identity,
            runtime_root=args.runtime_root,
            storage_root=args.storage_root or (args.storage_root_candidate[0] if args.storage_root_candidate else ""),
            records=records,
            storage_probe={"attempts": [], "threshold_bytes": HARD_MIN_FREE_BYTES},
        )
        print(json_dump(manifest))
        return 0
    finally:
        cleanup_synthetic_refs(workspace_root, records)


def run_sync(args: argparse.Namespace) -> int:
    workspace_root = repo_root_from(Path(args.workspace_root))
    snapshot_id = args.snapshot_id or now_utc().replace(":", "").replace("-", "") + "-" + uuid.uuid4().hex[:8]
    host = SshEndpoint(host=args.host, port=args.host_port, user=args.host_user)
    container = SshEndpoint(host=args.container_host, port=args.container_port, user=args.container_user)

    records = build_snapshot_records(workspace_root, args.workspace_id, snapshot_id, tuple(DEFAULT_DENYLIST))
    try:
        record_map = {record.relpath: record for record in records}
        reinstall_vllm = reinstall_required_for_repo(record_map["vllm"], VLLM_REINSTALL_PATTERNS) if "vllm" in record_map else False
        reinstall_vllm_ascend = reinstall_required_for_repo(record_map["vllm-ascend"], VLLM_ASCEND_REINSTALL_PATTERNS) if "vllm-ascend" in record_map else False

        runtime_state = load_runtime_state(workspace_root)
        previous_container_state = (
            runtime_state.get("servers", {})
            .get(args.server_name, {})
            .get("containers", {})
            .get(args.container_identity, {})
        )
        first_install = not bool(previous_container_state.get("first_reinstall_completed"))
        storage_root, storage_probe = choose_storage_root(
            repo_root=workspace_root,
            server_name=args.server_name,
            host=host,
            provided_root=args.storage_root,
            candidate_roots=args.storage_root_candidate,
            first_install=first_install,
            dry_run=args.dry_run,
        )

        reinstall_status = "not-needed"
        consent = resolve_install_consent(workspace_root, args.server_name, args.container_identity)
        if first_install:
            if consent != "allow":
                manifest = make_manifest(
                    workspace_root=workspace_root,
                    workspace_id=args.workspace_id,
                    snapshot_id=snapshot_id,
                    server_name=args.server_name,
                    container_identity=args.container_identity,
                    runtime_root=args.runtime_root,
                    storage_root=storage_root,
                    records=records,
                    storage_probe=storage_probe,
                )
                if args.print_manifest:
                    print(json_dump(manifest))
                summary = summary_payload(
                    status="blocked",
                    server_name=args.server_name,
                    container_identity=args.container_identity,
                    workspace_id=args.workspace_id,
                    storage_root=storage_root,
                    records=records,
                    reinstall_status="blocked-by-consent",
                    reason="first-time runtime replacement requires explicit consent",
                )
                print(json_dump(summary))
                return 2
            reinstall_vllm = True if "vllm" in record_map else reinstall_vllm
            reinstall_vllm_ascend = True if "vllm-ascend" in record_map else reinstall_vllm_ascend

        manifest = make_manifest(
            workspace_root=workspace_root,
            workspace_id=args.workspace_id,
            snapshot_id=snapshot_id,
            server_name=args.server_name,
            container_identity=args.container_identity,
            runtime_root=args.runtime_root,
            storage_root=storage_root,
            records=records,
            storage_probe=storage_probe,
        )

        if args.print_manifest:
            print(json_dump(manifest))

        lock_path = host_lock_path(storage_root, args.workspace_id, args.container_identity)
        try:
            acquire_host_lock(host, lock_path, args.dry_run)
            for record in records:
                mirror_path = mirror_path_for(storage_root, args.workspace_id, record)
                push_snapshot_to_mirror(
                    record=record,
                    repo=workspace_root if record.relpath in ("", ".") else workspace_root / record.relpath,
                    host=host,
                    mirror_path=mirror_path,
                    workspace_id=args.workspace_id,
                    dry_run=args.dry_run,
                )
            upload_manifest(host, host_manifest_path(storage_root, args.workspace_id, snapshot_id), manifest, args.dry_run)
            materialize_runtime(
                container=container,
                runtime_root=args.runtime_root,
                container_storage_root=args.container_storage_root or storage_root,
                workspace_id=args.workspace_id,
                records=records,
                dry_run=args.dry_run,
            )

            if reinstall_vllm or reinstall_vllm_ascend:
                reinstall_status = "performed"
                if not args.dry_run:
                    ssh_exec(
                        container,
                        runtime_install_script(
                            runtime_root=args.runtime_root,
                            reinstall_vllm=reinstall_vllm,
                            reinstall_vllm_ascend=reinstall_vllm_ascend,
                            container_identity=args.container_identity,
                        ),
                    )
            observed_runtime_commits = verify_runtime_commits(
                container=container,
                runtime_root=args.runtime_root,
                records=records,
                dry_run=args.dry_run,
            )
            expected_runtime_commits = {record.relpath: record.commit for record in records}
            if observed_runtime_commits != expected_runtime_commits:
                summary = summary_payload(
                    status="failed",
                    server_name=args.server_name,
                    container_identity=args.container_identity,
                    workspace_id=args.workspace_id,
                    storage_root=storage_root,
                    records=records,
                    reinstall_status=reinstall_status,
                    reason="runtime commit verification mismatch",
                    observed_runtime_commits=observed_runtime_commits,
                )
                print(json_dump(summary))
                return 1

            update_runtime_state(
                repo_root=workspace_root,
                server_name=args.server_name,
                container_identity=args.container_identity,
                runtime_root=args.runtime_root,
                storage_root=storage_root,
                records=records,
                first_reinstall_completed=first_install or previous_container_state.get("first_reinstall_completed", False) or reinstall_status == "performed",
            )
            summary = summary_payload(
                status="ready",
                server_name=args.server_name,
                container_identity=args.container_identity,
                workspace_id=args.workspace_id,
                storage_root=storage_root,
                records=records,
                reinstall_status=reinstall_status,
                reason=None,
                observed_runtime_commits=observed_runtime_commits,
            )
            print(json_dump(summary))
            return 0
        finally:
            release_host_lock(host, lock_path, args.dry_run)
    finally:
        cleanup_synthetic_refs(workspace_root, records)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare or enforce remote code parity for a ready runtime.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_shared_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--workspace-root", required=True, help="Local workspace root.")
        target.add_argument("--workspace-id", required=True, help="Stable workspace id used for remote cache namespacing.")
        target.add_argument("--server-name", required=True)
        target.add_argument("--runtime-root", required=True)
        target.add_argument("--container-identity", required=True)
        target.add_argument("--storage-root", default=None)
        target.add_argument("--storage-root-candidate", action="append", default=[])

    plan = subparsers.add_parser("plan", help="Build a synthetic snapshot manifest without remote mutations.")
    add_shared_arguments(plan)
    plan.add_argument("--snapshot-id", default=None)
    plan.add_argument("--print-manifest", action="store_true")

    sync = subparsers.add_parser("sync", help="Publish mirrors, materialize runtime state, and reinstall when required.")
    add_shared_arguments(sync)
    sync.add_argument("--snapshot-id", default=None)
    sync.add_argument("--host", required=True)
    sync.add_argument("--host-port", type=int, default=22)
    sync.add_argument("--host-user", required=True)
    sync.add_argument("--container-host", required=True)
    sync.add_argument("--container-port", type=int, required=True)
    sync.add_argument("--container-user", required=True)
    sync.add_argument("--container-storage-root", default=None, help="Container-visible path for the host storage root. Defaults to --storage-root.")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--print-manifest", action="store_true")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "plan":
            return run_plan(args)
        if args.command == "sync":
            return run_sync(args)
        parser.error(f"unsupported command: {args.command}")
        return 2
    except Exception as exc:
        payload: dict[str, Any] = {
            "status": "failed",
            "reason": str(exc),
        }
        for field in ("server_name", "container_identity", "workspace_id"):
            if hasattr(args, field):
                payload[field] = getattr(args, field)
        print(json_dump(payload))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
