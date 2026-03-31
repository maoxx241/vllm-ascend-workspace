import configparser
import json
from pathlib import Path
from typing import Optional

from .config import RepoPaths

OVERLAY_FILES = ("targets.yaml", "repos.yaml", "auth.yaml", "state.json")


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

    state_file = paths.local_overlay / "state.json"
    try:
        json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        print("invalid state file: .workspace.local/state.json is not valid JSON")
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

    paths.local_overlay.mkdir(parents=True, exist_ok=True)
    for name in OVERLAY_FILES:
        file_path = paths.local_overlay / name
        if not file_path.exists():
            if name == "state.json":
                file_path.write_text("{}\n", encoding="utf-8")
            else:
                file_path.write_text("", encoding="utf-8")

    print("init: ok")
    return 0
