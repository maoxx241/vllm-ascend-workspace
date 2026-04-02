from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass
from typing import Any, Dict, List

from .config import RepoPaths
from .remote import _stream_repo_to_host, resolve_server_context, run_runtime_command
from .repo_targets import WorkspaceTargets
from .runtime import read_state, require_container_ssh_transport, update_state


@dataclass(frozen=True)
class CodeParityResult:
    status: str
    summary: str
    mismatches: List[str]
    desired_state: Dict[str, Any]
    remote_state: Dict[str, Any]

    def to_mapping(self) -> Dict[str, Any]:
        return asdict(self)


def _desired_state_mapping(desired: WorkspaceTargets) -> Dict[str, Any]:
    return {
        "workspace": desired.workspace.to_mapping(),
        "vllm": desired.vllm.to_mapping(),
        "vllm-ascend": desired.vllm_ascend.to_mapping(),
    }


def build_materialization_script(desired: WorkspaceTargets, *, workspace_root: str) -> str:
    workspace_path = f"{workspace_root}/workspace"
    return "\n".join(
        [
            "set -euo pipefail",
            f"git -C {shlex.quote(workspace_path)} checkout --detach {shlex.quote(desired.workspace.commit)}",
            f"git -C {shlex.quote(workspace_path + '/vllm')} checkout --detach {shlex.quote(desired.vllm.commit)}",
            f"git -C {shlex.quote(workspace_path + '/vllm-ascend')} checkout --detach {shlex.quote(desired.vllm_ascend.commit)}",
        ]
    )


def sync_workspace_mirror(paths: RepoPaths, server_name: str) -> None:
    ctx = resolve_server_context(paths, server_name)
    _stream_repo_to_host(paths, ctx)


def _collect_remote_git_state(paths: RepoPaths, server_name: str) -> Dict[str, Any]:
    ctx = resolve_server_context(paths, server_name)
    transport = require_container_ssh_transport(paths, server_name)
    workspace_path = f"{ctx.runtime.workspace_root}/workspace"
    script = "\n".join(
        [
            "set -euo pipefail",
            f"for entry in 'workspace|{workspace_path}' 'vllm|{workspace_path}/vllm' 'vllm-ascend|{workspace_path}/vllm-ascend'; do",
            "  name=${entry%%|*}",
            "  repo=${entry#*|}",
            "  if [ -e \"$repo/.git\" ] || [ -d \"$repo/.git\" ]; then",
            "    commit=$(git -C \"$repo\" rev-parse HEAD)",
            "  else",
            "    commit=missing",
            "  fi",
            "  printf '%s\\t%s\\n' \"$name\" \"$commit\"",
            "done",
        ]
    )
    result = run_runtime_command(ctx, transport, script)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())

    remote_state: Dict[str, Any] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, commit = line.split("\t", 1)
        remote_state[name] = {"commit": commit}
    return remote_state


def _persist_code_parity(paths: RepoPaths, server_name: str, result: CodeParityResult) -> None:
    state = read_state(paths)
    existing = state.get("code_parity")
    if not isinstance(existing, dict):
        existing = {}
    existing[server_name] = result.to_mapping()
    update_state(paths, code_parity=existing)


def verify_code_parity(
    paths: RepoPaths,
    server_name: str,
    desired: WorkspaceTargets,
) -> CodeParityResult:
    remote_state = _collect_remote_git_state(paths, server_name)
    desired_state = _desired_state_mapping(desired)

    mismatches: List[str] = []
    for repo_name, target in desired_state.items():
        expected_commit = target["commit"]
        actual = remote_state.get(repo_name)
        actual_commit = actual.get("commit") if isinstance(actual, dict) else None
        if actual_commit != expected_commit:
            mismatches.append(repo_name)

    if mismatches:
        result = CodeParityResult(
            status="needs_repair",
            summary=f"remote code parity mismatch for {server_name}: {', '.join(mismatches)}",
            mismatches=mismatches,
            desired_state=desired_state,
            remote_state=remote_state,
        )
    else:
        result = CodeParityResult(
            status="ready",
            summary=f"remote code parity ready for {server_name}",
            mismatches=[],
            desired_state=desired_state,
            remote_state=remote_state,
        )

    _persist_code_parity(paths, server_name, result)
    return result


def ensure_code_parity(
    paths: RepoPaths,
    server_name: str,
    desired: WorkspaceTargets,
) -> CodeParityResult:
    ctx = resolve_server_context(paths, server_name)
    sync_workspace_mirror(paths, server_name)
    transport = require_container_ssh_transport(paths, server_name)
    script = build_materialization_script(
        desired,
        workspace_root=ctx.runtime.workspace_root,
    )
    result = run_runtime_command(ctx, transport, script)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return verify_code_parity(paths, server_name, desired)
