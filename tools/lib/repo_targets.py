from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import RepoPaths


@dataclass(frozen=True)
class RepoTarget:
    repo_name: str
    repo_path: Path
    origin_url: str
    upstream_url: str
    remote_name: str
    ref_name: str
    commit: str
    branch_name: str | None

    def to_mapping(self) -> dict[str, object]:
        payload = asdict(self)
        payload["repo_path"] = str(self.repo_path)
        return payload


@dataclass(frozen=True)
class WorkspaceTargets:
    workspace: RepoTarget
    vllm: RepoTarget
    vllm_ascend: RepoTarget

    def to_mapping(self) -> dict[str, object]:
        return {
            "workspace": self.workspace.to_mapping(),
            "vllm": self.vllm.to_mapping(),
            "vllm_ascend": self.vllm_ascend.to_mapping(),
        }


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return result.stdout.strip()


def _current_branch(repo_path: Path) -> str | None:
    branch_name = _git(repo_path, "branch", "--show-current")
    return branch_name or None


def _current_commit(repo_path: Path) -> str:
    return _git(repo_path, "rev-parse", "HEAD")


def _remote_url(repo_path: Path, remote_name: str) -> str:
    return _git(repo_path, "remote", "get-url", remote_name)


def _resolve_commit(repo_path: Path, ref_name: str) -> str:
    return _git(repo_path, "rev-parse", f"{ref_name}^{{commit}}")


def _default_ref_name(repo_path: Path, branch_name: str | None) -> tuple[str, str]:
    if branch_name:
        return "origin", f"refs/heads/{branch_name}"
    head_commit = _current_commit(repo_path)
    return "origin", head_commit


def _resolve_repo_target(
    repo_path: Path,
    *,
    repo_name: str,
    remote_name: str | None = None,
    ref_name: str | None = None,
) -> RepoTarget:
    branch_name = _current_branch(repo_path)
    resolved_remote_name = remote_name
    resolved_ref_name = ref_name
    if resolved_remote_name is None or resolved_ref_name is None:
        default_remote_name, default_ref_name = _default_ref_name(repo_path, branch_name)
        if resolved_remote_name is None:
            resolved_remote_name = default_remote_name
        if resolved_ref_name is None:
            resolved_ref_name = default_ref_name

    return RepoTarget(
        repo_name=repo_name,
        repo_path=repo_path,
        origin_url=_remote_url(repo_path, "origin"),
        upstream_url=_remote_url(repo_path, "upstream"),
        remote_name=resolved_remote_name,
        ref_name=resolved_ref_name,
        commit=_resolve_commit(repo_path, resolved_ref_name),
        branch_name=branch_name,
    )


def resolve_repo_targets(
    paths: RepoPaths,
    *,
    vllm_upstream_tag: str | None,
    vllm_ascend_upstream_branch: str | None,
    require_feature_branch: bool = False,
) -> WorkspaceTargets:
    workspace_branch = _current_branch(paths.root)
    if require_feature_branch and workspace_branch in {"main", "master"}:
        raise RuntimeError(
            "workspace is still on a protected branch; create a feature branch first"
        )

    workspace = _resolve_repo_target(
        paths.root,
        repo_name="workspace",
        remote_name="origin",
        ref_name=f"refs/heads/{workspace_branch}" if workspace_branch else None,
    )

    vllm = _resolve_repo_target(
        paths.root / "vllm",
        repo_name="vllm",
        remote_name="upstream" if vllm_upstream_tag else None,
        ref_name=f"refs/tags/{vllm_upstream_tag}" if vllm_upstream_tag else None,
    )

    vllm_ascend = _resolve_repo_target(
        paths.root / "vllm-ascend",
        repo_name="vllm-ascend",
        remote_name="upstream" if vllm_ascend_upstream_branch else None,
        ref_name=(
            f"refs/remotes/upstream/{vllm_ascend_upstream_branch}"
            if vllm_ascend_upstream_branch
            else None
        ),
    )

    return WorkspaceTargets(
        workspace=workspace,
        vllm=vllm,
        vllm_ascend=vllm_ascend,
    )
