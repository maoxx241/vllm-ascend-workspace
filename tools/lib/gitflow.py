from typing import Any, Dict

import yaml

from .config import RepoPaths


def _read_overlay_repos(paths: RepoPaths) -> Dict[str, Any]:
    repos_file = paths.local_overlay / "repos.yaml"
    if not repos_file.is_file():
        return {}

    try:
        loaded = yaml.safe_load(repos_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}

    if not isinstance(loaded, dict):
        return {}
    return loaded


def default_base_ref(paths: RepoPaths) -> str:
    config = _read_overlay_repos(paths)
    workspace = config.get("workspace")
    if not isinstance(workspace, dict):
        return "origin/main"

    default_branch = workspace.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        return "origin/main"

    push_remote = workspace.get("push_remote")
    if not isinstance(push_remote, str) or not push_remote.strip():
        push_remote = "origin"

    return f"{push_remote.strip()}/{default_branch.strip()}"
