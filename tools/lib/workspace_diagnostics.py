from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import RepoPaths
from .git_auth import probe_git_auth
from .repo_targets import describe_repo_targets
from .repo_topology import probe_repo_topology, probe_submodules

REQUIRED_OVERLAY_FILES = ("servers.yaml", "auth.yaml", "repos.yaml")


def _load_yaml_mapping(path: Path, invalid_message: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeError(invalid_message) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError(invalid_message)
    return loaded


def diagnose_overlay(paths: RepoPaths) -> dict[str, object]:
    if not paths.local_overlay.exists():
        return {
            "status": "needs_repair",
            "summary": "missing local overlay",
            "observations": ["missing local overlay: .workspace.local/"],
            "payload": {"required_files": list(REQUIRED_OVERLAY_FILES)},
        }
    if not paths.local_overlay.is_dir():
        return {
            "status": "needs_repair",
            "summary": "invalid local overlay",
            "observations": ["invalid local overlay: .workspace.local/ must be a directory"],
            "payload": {"required_files": list(REQUIRED_OVERLAY_FILES)},
        }

    problems: list[str] = []
    for filename in REQUIRED_OVERLAY_FILES:
        path = paths.local_overlay / filename
        if not path.is_file():
            problems.append(f"missing overlay file: .workspace.local/{filename}")
            continue
        try:
            _load_yaml_mapping(
                path,
                f"invalid overlay file: .workspace.local/{filename}",
            )
        except RuntimeError as exc:
            problems.append(str(exc))

    if problems:
        return {
            "status": "needs_repair",
            "summary": "overlay files need repair",
            "observations": problems,
            "payload": {"required_files": list(REQUIRED_OVERLAY_FILES)},
        }

    return {
        "status": "ready",
        "summary": "overlay config files are present and parseable",
        "observations": ["overlay config files are present and parseable"],
        "payload": {"required_files": list(REQUIRED_OVERLAY_FILES)},
    }


def diagnose_workspace(paths: RepoPaths) -> dict[str, object]:
    overlay = diagnose_overlay(paths)
    topology = probe_repo_topology(paths)
    submodules = probe_submodules(paths)
    git_auth = probe_git_auth()

    repo_targets_error: str | None = None
    repo_targets: dict[str, object] = {}
    try:
        repo_targets = describe_repo_targets(paths)
    except RuntimeError as exc:
        repo_targets_error = str(exc)

    observations: list[str] = []
    if overlay["status"] != "ready":
        observations.extend(str(item) for item in overlay["observations"])
    if topology["status"] != "ready":
        observations.append(str(topology["detail"]))
    if submodules["status"] != "ready":
        observations.append(str(submodules["detail"]))
    if repo_targets_error:
        observations.append(f"repo targets unavailable: {repo_targets_error}")

    if observations:
        return {
            "status": "needs_repair",
            "summary": "workspace diagnostics found issues",
            "observations": observations,
            "payload": {
                "overlay": overlay,
                "repo_topology": topology,
                "submodules": submodules,
                "git_auth": git_auth,
                "repo_targets": repo_targets,
            },
        }

    return {
        "status": "ready",
        "summary": "workspace diagnostics ready",
        "observations": ["workspace diagnostics ready"],
        "payload": {
            "overlay": overlay,
            "repo_topology": topology,
            "submodules": submodules,
            "git_auth": git_auth,
            "repo_targets": repo_targets,
        },
    }
