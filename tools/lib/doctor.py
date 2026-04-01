import configparser
import json
from pathlib import Path
from typing import Optional

import yaml

from .config import RepoPaths
from .overlay import OVERLAY_SCHEMA_VERSION, ensure_overlay_layout

OVERLAY_FILES = ("servers.yaml", "repos.yaml", "auth.yaml", "state.json")
BOOTSTRAP_MODES = {"remote-first", "server", "local-only"}


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


def _validate_lifecycle_shape(state: dict) -> None:
    lifecycle = state.get("lifecycle")
    if lifecycle is not None and not isinstance(lifecycle, dict):
        raise ValueError(
            "invalid state file: .workspace.local/state.json lifecycle must be an object"
        )


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

    servers_file = paths.local_servers_file
    try:
        servers_config = yaml.safe_load(servers_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        print("invalid servers file: .workspace.local/servers.yaml is not valid YAML")
        return 1

    if not isinstance(servers_config, dict):
        print("invalid servers file: .workspace.local/servers.yaml must be a YAML mapping")
        return 1

    bootstrap_config = servers_config.get("bootstrap")
    if bootstrap_config is not None:
        if not isinstance(bootstrap_config, dict):
            print("invalid servers file: .workspace.local/servers.yaml bootstrap must be a mapping")
            return 1
        mode = bootstrap_config.get("mode")
        if mode is not None and mode not in BOOTSTRAP_MODES:
            print("invalid servers file: .workspace.local/servers.yaml bootstrap mode is unsupported")
            return 1
        completed = bootstrap_config.get("completed")
        if completed is not None and not isinstance(completed, bool):
            print("invalid servers file: .workspace.local/servers.yaml bootstrap completed must be a boolean")
            return 1

    state_file = paths.local_state_file
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        print("invalid state file: .workspace.local/state.json is not valid JSON")
        return 1

    if not isinstance(state, dict):
        print("invalid state file: .workspace.local/state.json must be a JSON object")
        return 1

    try:
        _validate_lifecycle_shape(state)
    except ValueError as exc:
        print(str(exc))
        return 1

    schema_version = state.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        print("invalid state file: .workspace.local/state.json missing schema_version")
        return 1
    if schema_version != OVERLAY_SCHEMA_VERSION:
        print(
            "unsupported schema_version in "
            ".workspace.local/state.json: "
            f"{schema_version}"
        )
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
