from __future__ import annotations

from .bootstrap import (
    BootstrapError,
    _ensure_overlay,
    configure_repo_remotes,
    load_existing_repo_topology,
    topology_is_ready,
    write_git_auth_ref,
    write_repo_topology,
)
from .config import RepoPaths
from .lifecycle_state import record_git_profile_status


def git_profile(
    paths: RepoPaths,
    *,
    vllm_origin_url: str | None = None,
    vllm_ascend_origin_url: str | None = None,
) -> int:
    try:
        _ensure_overlay(paths)
        existing_topology = load_existing_repo_topology(paths)
        if existing_topology and topology_is_ready(paths):
            write_git_auth_ref(
                paths,
                git_auth_mode="ssh-agent",
                git_key_path=None,
                git_token_env=None,
            )
            record_git_profile_status(paths, "ready")
            print("git-profile: ready")
            return 0

        if not vllm_ascend_origin_url:
            record_git_profile_status(paths, "needs_input")
            print("git-profile: needs_input: missing vllm-ascend origin url")
            return 1

        resolved_vllm_origin_url = vllm_origin_url
        resolved_vllm_ascend_origin_url = vllm_ascend_origin_url

        write_repo_topology(
            paths,
            vllm_origin_url=resolved_vllm_origin_url,
            vllm_ascend_origin_url=resolved_vllm_ascend_origin_url,
        )
        configure_repo_remotes(
            paths,
            vllm_origin_url=resolved_vllm_origin_url,
            vllm_ascend_origin_url=resolved_vllm_ascend_origin_url,
        )
        write_git_auth_ref(
            paths,
            git_auth_mode="ssh-agent",
            git_key_path=None,
            git_token_env=None,
        )
        record_git_profile_status(paths, "ready")
        print("git-profile: ready")
        return 0
    except BootstrapError as exc:
        print(str(exc))
        return 1
    except RuntimeError as exc:
        print(str(exc))
        return 1
