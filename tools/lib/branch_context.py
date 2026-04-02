from __future__ import annotations

import subprocess
from typing import Iterable

import yaml

from .config import RepoPaths


def _git(repo_path, *args: str) -> str:
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


def _current_branch(paths: RepoPaths) -> str | None:
    branch = _git(paths.root, "branch", "--show-current")
    return branch or None


def _is_detached_head(paths: RepoPaths) -> bool:
    return _git(paths.root, "rev-parse", "--symbolic-full-name", "HEAD") == "HEAD"


def _protected_branches(paths: RepoPaths) -> set[str]:
    try:
        loaded = yaml.safe_load(paths.local_repos_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        loaded = {}
    workspace = loaded.get("workspace") if isinstance(loaded, dict) else None
    protected = workspace.get("protected_branches") if isinstance(workspace, dict) else None
    if isinstance(protected, Iterable) and not isinstance(protected, (str, bytes, dict)):
        values = {
            str(branch).strip()
            for branch in protected
            if isinstance(branch, str) and branch.strip()
        }
        if values:
            return values
    return {"main"}


def require_feature_branch(paths: RepoPaths) -> None:
    branch = _current_branch(paths)
    protected = _protected_branches(paths)
    if _is_detached_head(paths):
        raise RuntimeError("workspace is still on a protected branch; create a feature branch first")
    if not branch:
        raise RuntimeError("workspace is still on a protected branch; create a feature branch first")
    normalized = branch.removeprefix("origin/")
    if branch in protected or normalized in protected:
        raise RuntimeError("workspace is still on a protected branch; create a feature branch first")
