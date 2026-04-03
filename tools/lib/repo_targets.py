from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .branch_context import require_feature_branch as require_workspace_feature_branch
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


def _ref_exists(repo_path: Path, ref_name: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref_name}^{{commit}}"],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _fetch_explicit_ref(repo_path: Path, remote_name: str, ref_name: str) -> None:
    if ref_name.startswith("refs/tags/"):
        tag_name = ref_name.removeprefix("refs/tags/")
        _git(
            repo_path,
            "fetch",
            remote_name,
            f"refs/tags/{tag_name}:refs/tags/{tag_name}",
        )
        return

    remote_prefix = f"refs/remotes/{remote_name}/"
    if ref_name.startswith(remote_prefix):
        branch_name = ref_name.removeprefix(remote_prefix)
        _git(
            repo_path,
            "fetch",
            remote_name,
            f"refs/heads/{branch_name}:{ref_name}",
        )
        return

    _git(repo_path, "fetch", remote_name)


def _resolve_commit(
    repo_path: Path,
    ref_name: str,
    *,
    remote_name: str | None,
    fetch_missing: bool,
) -> str:
    if not _ref_exists(repo_path, ref_name):
        if not fetch_missing or remote_name is None:
            return _git(repo_path, "rev-parse", f"{ref_name}^{{commit}}")
        _fetch_explicit_ref(repo_path, remote_name, ref_name)
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
    fetch_missing: bool = False,
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
        commit=_resolve_commit(
            repo_path,
            resolved_ref_name,
            remote_name=resolved_remote_name,
            fetch_missing=fetch_missing,
        ),
        branch_name=branch_name,
    )


def _checkout_detached(repo_path: Path, commit: str) -> None:
    _git(repo_path, "checkout", "--detach", commit)


def resolve_repo_targets(
    paths: RepoPaths,
    *,
    vllm_upstream_tag: str | None,
    vllm_upstream_branch: str | None = None,
    vllm_ascend_upstream_branch: str | None,
    require_feature_branch: bool = False,
    fetch_missing: bool = False,
) -> WorkspaceTargets:
    if require_feature_branch:
        require_workspace_feature_branch(paths)

    workspace_branch = _current_branch(paths.root)

    workspace = _resolve_repo_target(
        paths.root,
        repo_name="workspace",
        remote_name="origin",
        ref_name=f"refs/heads/{workspace_branch}" if workspace_branch else None,
        fetch_missing=fetch_missing,
    )

    vllm = _resolve_repo_target(
        paths.root / "vllm",
        repo_name="vllm",
        remote_name="upstream" if (vllm_upstream_tag or vllm_upstream_branch) else None,
        ref_name=(
            f"refs/tags/{vllm_upstream_tag}"
            if vllm_upstream_tag
            else (
                f"refs/remotes/upstream/{vllm_upstream_branch}"
                if vllm_upstream_branch
                else None
            )
        ),
        fetch_missing=fetch_missing,
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
        fetch_missing=fetch_missing,
    )

    return WorkspaceTargets(
        workspace=workspace,
        vllm=vllm,
        vllm_ascend=vllm_ascend,
    )


def materialize_workspace_targets(
    paths: RepoPaths,
    *,
    vllm_upstream_tag: str | None,
    vllm_ascend_upstream_branch: str | None,
    vllm_upstream_branch: str | None = None,
) -> WorkspaceTargets:
    targets = resolve_repo_targets(
        paths,
        vllm_upstream_tag=vllm_upstream_tag,
        vllm_upstream_branch=vllm_upstream_branch,
        vllm_ascend_upstream_branch=vllm_ascend_upstream_branch,
        fetch_missing=True,
    )
    _checkout_detached(paths.root / "vllm", targets.vllm.commit)
    _checkout_detached(paths.root / "vllm-ascend", targets.vllm_ascend.commit)
    return targets


def describe_repo_targets(paths: RepoPaths) -> dict[str, object]:
    targets = resolve_repo_targets(
        paths,
        vllm_upstream_tag=None,
        vllm_upstream_branch=None,
        vllm_ascend_upstream_branch=None,
        fetch_missing=False,
    )
    return {
        "workspace": targets.workspace.to_mapping(),
        "vllm": targets.vllm.to_mapping(),
        "vllm_ascend": targets.vllm_ascend.to_mapping(),
    }
