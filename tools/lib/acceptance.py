from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .benchmark import run_benchmark_preset
from .code_parity import ensure_code_parity
from .config import RepoPaths
from .repo_targets import (
    WorkspaceTargets,
    materialize_workspace_targets,
    resolve_repo_targets,
)
from .runtime_env import ensure_runtime_environment


@dataclass(frozen=True)
class AcceptanceRequest:
    server_name: str
    vllm_origin_url: Optional[str]
    vllm_ascend_origin_url: Optional[str]
    vllm_upstream_tag: Optional[str]
    vllm_ascend_upstream_branch: str
    benchmark_preset: str


def _requested_targets(paths: RepoPaths, request: AcceptanceRequest) -> WorkspaceTargets:
    return resolve_repo_targets(
        paths,
        vllm_upstream_tag=request.vllm_upstream_tag,
        vllm_ascend_upstream_branch=request.vllm_ascend_upstream_branch,
    )


def ensure_remote_baseline_for_acceptance(paths: RepoPaths, request: AcceptanceRequest) -> int:
    try:
        baseline = resolve_repo_targets(
            paths,
            vllm_upstream_tag=None,
            vllm_upstream_branch="main",
            vllm_ascend_upstream_branch="main",
            fetch_missing=True,
        )
        result = ensure_code_parity(paths, request.server_name, baseline)
        print(result.summary)
        if result.status != "ready":
            return 1
        env_result = ensure_runtime_environment(paths, request.server_name, baseline)
        print(env_result.summary)
        return 0 if env_result.status == "ready" else 1
    except RuntimeError as exc:
        print(str(exc))
        return 1


def materialize_requested_targets_for_acceptance(
    paths: RepoPaths,
    request: AcceptanceRequest,
) -> WorkspaceTargets:
    return materialize_workspace_targets(
        paths,
        vllm_upstream_tag=request.vllm_upstream_tag,
        vllm_ascend_upstream_branch=request.vllm_ascend_upstream_branch,
    )


def ensure_code_parity_for_acceptance(
    paths: RepoPaths,
    request: AcceptanceRequest,
    desired: WorkspaceTargets,
) -> int:
    result = ensure_code_parity(paths, request.server_name, desired)
    print(result.summary)
    return 0 if result.status == "ready" else 1


def ensure_runtime_environment_for_acceptance(
    paths: RepoPaths,
    request: AcceptanceRequest,
    desired: WorkspaceTargets,
) -> int:
    result = ensure_runtime_environment(paths, request.server_name, desired)
    print(result.summary)
    return 0 if result.status == "ready" else 1


def run_benchmark_for_acceptance(paths: RepoPaths, request: AcceptanceRequest) -> int:
    return run_benchmark_preset(paths, request.server_name, request.benchmark_preset)


def run_acceptance(root: Path, request: AcceptanceRequest) -> int:
    paths = RepoPaths(root=root)
    try:
        if ensure_remote_baseline_for_acceptance(paths, request) != 0:
            return 1
        desired = materialize_requested_targets_for_acceptance(paths, request)
        if ensure_code_parity_for_acceptance(paths, request, desired) != 0:
            return 1
        if ensure_runtime_environment_for_acceptance(paths, request, desired) != 0:
            return 1
        return run_benchmark_for_acceptance(paths, request)
    except RuntimeError as exc:
        print(str(exc))
        return 1
