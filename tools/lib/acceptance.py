from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .benchmark import run_benchmark_preset
from .code_parity import ensure_code_parity
from .config import RepoPaths
from .init_flow import InitRequest, run_init
from .repo_targets import resolve_repo_targets


@dataclass(frozen=True)
class AcceptanceRequest:
    server_name: str
    server_host: str
    server_user: str
    server_password_env: Optional[str]
    vllm_origin_url: Optional[str]
    vllm_ascend_origin_url: Optional[str]
    vllm_upstream_tag: Optional[str]
    vllm_ascend_upstream_branch: str
    benchmark_preset: str


def ensure_code_parity_for_acceptance(paths: RepoPaths, request: AcceptanceRequest) -> int:
    desired = resolve_repo_targets(
        paths,
        vllm_upstream_tag=request.vllm_upstream_tag,
        vllm_ascend_upstream_branch=request.vllm_ascend_upstream_branch,
    )
    result = ensure_code_parity(paths, request.server_name, desired)
    print(result.summary)
    return 0 if result.status == "ready" else 1


def run_benchmark_for_acceptance(paths: RepoPaths, request: AcceptanceRequest) -> int:
    return run_benchmark_preset(paths, request.server_name, request.benchmark_preset)


def run_acceptance(root: Path, request: AcceptanceRequest) -> int:
    paths = RepoPaths(root=root)
    init_request = InitRequest(
        server_host=request.server_host,
        server_name=request.server_name,
        server_user=request.server_user,
        server_password_env=request.server_password_env,
        vllm_origin_url=request.vllm_origin_url,
        vllm_ascend_origin_url=request.vllm_ascend_origin_url,
        vllm_upstream_tag=request.vllm_upstream_tag,
        vllm_ascend_upstream_branch=request.vllm_ascend_upstream_branch,
    )
    if run_init(paths, init_request) != 0:
        return 1
    if ensure_code_parity_for_acceptance(paths, request) != 0:
        return 1
    return run_benchmark_for_acceptance(paths, request)
