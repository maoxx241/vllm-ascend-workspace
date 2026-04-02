import configparser
import subprocess
from pathlib import Path
from typing import Dict, Optional

import yaml

from .capability_state import diagnose_state_residue, read_capability_state
from .config import RepoPaths
from .overlay import ensure_overlay_layout

OVERLAY_FILES = ("servers.yaml", "repos.yaml", "auth.yaml", "state.json")


def _declared_submodule_paths(root: Path):
    gitmodules = root / ".gitmodules"
    if not gitmodules.is_file():
        return None

    parser = configparser.ConfigParser()
    try:
        parser.read(gitmodules, encoding="utf-8")
    except configparser.Error:
        return []

    paths = []
    for section in parser.sections():
        if not section.startswith("submodule "):
            continue
        path_value = parser.get(section, "path", fallback="").strip()
        if path_value:
            paths.append(Path(path_value))
    return paths


def _missing_submodules(root: Path, prefix: Optional[Path] = None):
    declared = _declared_submodule_paths(root)
    if declared is None:
        if prefix is None:
            return [".gitmodules"]
        return []

    missing = []
    for relative_path in declared:
        full_path = root / relative_path
        display_path = relative_path if prefix is None else prefix / relative_path
        git_marker = full_path / ".git"
        if not full_path.exists() or not git_marker.exists():
            missing.append(str(display_path))
            continue
        missing.extend(_missing_submodules(full_path, display_path))
    return missing


def _git_remote_url(repo_path: Path, remote_name: str) -> Optional[str]:
    result = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _is_placeholder_remote(url: Optional[str]) -> bool:
    if not isinstance(url, str) or not url.strip():
        return True
    stripped = url.strip().lower()
    return "your-org/" in stripped or "your-org:" in stripped or "example/" in stripped


def _has_placeholder_workspace_remote(paths: RepoPaths) -> bool:
    return _is_placeholder_remote(_git_remote_url(paths.root, "origin")) or _is_placeholder_remote(
        _git_remote_url(paths.root, "upstream")
    )


def _load_yaml_mapping(path: Path, invalid_message: str) -> Dict[str, object]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise RuntimeError(invalid_message)
    if not isinstance(loaded, dict):
        raise RuntimeError(invalid_message)
    return loaded


def _legacy_inventory_residue(paths: RepoPaths, servers_config: Dict[str, object]) -> list[str]:
    residue = list(diagnose_state_residue(paths))
    if paths.local_targets_file.exists():
        residue.append("legacy overlay residue: .workspace.local/targets.yaml")

    if "bootstrap" in servers_config:
        residue.append("legacy server inventory residue: bootstrap")

    servers = servers_config.get("servers")
    if isinstance(servers, dict):
        for server_name, server in servers.items():
            if isinstance(server, dict) and "verification" in server:
                residue.append(f"legacy server inventory residue: servers.{server_name}.verification")
    return residue


def doctor(paths: RepoPaths) -> int:
    if not paths.local_overlay.exists():
        print("missing local overlay: .workspace.local/")
        return 1
    if not paths.local_overlay.is_dir():
        print("invalid local overlay: .workspace.local/ must be a directory")
        return 1

    missing_files = [
        name for name in OVERLAY_FILES if not (paths.local_overlay / name).is_file()
    ]
    if missing_files:
        print(f"missing overlay files: {', '.join(missing_files)}")
        return 1

    try:
        servers_config = _load_yaml_mapping(
            paths.local_servers_file,
            "invalid servers file: .workspace.local/servers.yaml must be a YAML mapping",
        )
        _load_yaml_mapping(
            paths.local_auth_file,
            "invalid auth file: .workspace.local/auth.yaml must be a YAML mapping",
        )
        _load_yaml_mapping(
            paths.local_repos_file,
            "invalid repos file: .workspace.local/repos.yaml must be a YAML mapping",
        )
    except RuntimeError as exc:
        print(str(exc))
        return 1

    problems = _legacy_inventory_residue(paths, servers_config)
    if _has_placeholder_workspace_remote(paths):
        problems.append("placeholder_workspace_remote")

    if not problems:
        try:
            read_capability_state(paths)
        except RuntimeError as exc:
            problems.append(str(exc))

    if problems:
        print("\n".join(problems))
        return 1

    missing_submodules = _missing_submodules(paths.root)
    if missing_submodules:
        print(f"missing initialized submodules: {', '.join(missing_submodules)}")
        return 1

    print("doctor: ok")
    return 0


def init(paths: RepoPaths) -> int:
    if paths.local_overlay.exists() and not paths.local_overlay.is_dir():
        print("invalid local overlay: .workspace.local/ exists but is not a directory")
        return 1

    ensure_overlay_layout(paths)

    print("init: ok")
    return 0
