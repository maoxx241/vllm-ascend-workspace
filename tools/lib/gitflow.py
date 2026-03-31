from typing import Any, Dict

import yaml

from .config import RepoPaths


def _read_overlay_repos(paths: RepoPaths) -> Dict[str, Any]:
    repos_file = paths.local_overlay / "repos.yaml"
    if not repos_file.is_file():
        return {}

    try:
        loaded = yaml.safe_load(repos_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(
            "cannot read workspace config: .workspace.local/repos.yaml"
        ) from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            "invalid workspace config: .workspace.local/repos.yaml"
        ) from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(
            "invalid workspace config: .workspace.local/repos.yaml"
        ) from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError("invalid workspace config: .workspace.local/repos.yaml")
    return loaded


def _normalize_base_ref(section: Dict[str, Any]) -> str:
    if "default_branch" not in section:
        default_branch = "main"
    else:
        default_branch = section["default_branch"]
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise RuntimeError("invalid workspace config: .workspace.local/repos.yaml")

    if "push_remote" not in section:
        push_remote = "origin"
    else:
        push_remote = section["push_remote"]
        if not isinstance(push_remote, str) or not push_remote.strip():
            raise RuntimeError("invalid workspace config: .workspace.local/repos.yaml")

    return f"{push_remote.strip()}/{default_branch.strip()}"


def base_ref_for_repo(paths: RepoPaths, repo_name: str = "workspace") -> str:
    config = _read_overlay_repos(paths)
    if repo_name == "workspace":
        if "workspace" not in config:
            return "origin/main"
        workspace = config["workspace"]
        if not isinstance(workspace, dict):
            raise RuntimeError("invalid workspace config: .workspace.local/repos.yaml")
        return _normalize_base_ref(workspace)

    submodules = config.get("submodules")
    if submodules is None:
        return base_ref_for_repo(paths, "workspace")
    if not isinstance(submodules, dict):
        raise RuntimeError("invalid workspace config: .workspace.local/repos.yaml")

    repo_config = submodules.get(repo_name)
    if repo_config is None:
        return base_ref_for_repo(paths, "workspace")
    if not isinstance(repo_config, dict):
        raise RuntimeError("invalid workspace config: .workspace.local/repos.yaml")
    return _normalize_base_ref(repo_config)


def default_base_ref(paths: RepoPaths) -> str:
    return base_ref_for_repo(paths, "workspace")
