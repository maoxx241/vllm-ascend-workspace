from pathlib import Path

import yaml

from .config import RepoPaths
from .runtime import read_state, write_state


def _session_manifest_path(paths: RepoPaths, session_name: str) -> Path:
    return paths.local_overlay / "sessions" / session_name / "manifest.yaml"


def create_session(paths: RepoPaths, session_name: str) -> int:
    try:
        state = read_state(paths)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    manifest = {
        "name": session_name,
        "workspace_root": "/vllm-workspace",
        "base_ref": "origin/main",
    }
    current_target = state.get("current_target")
    if isinstance(current_target, str) and current_target.strip():
        manifest["target"] = current_target

    manifest_path = _session_manifest_path(paths, session_name)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    print(f"session create: ok ({session_name})")
    return 0


def switch_session(paths: RepoPaths, session_name: str) -> int:
    manifest_path = _session_manifest_path(paths, session_name)
    if not manifest_path.is_file():
        print(f"unknown session: {session_name}")
        return 1

    try:
        state = read_state(paths)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    state["current_session"] = session_name
    write_state(paths, state)
    print(f"session switch: ok ({session_name})")
    return 0


def status_session(paths: RepoPaths) -> int:
    try:
        state = read_state(paths)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    current_session = state.get("current_session")
    if isinstance(current_session, str) and current_session.strip():
        print(f"current session: {current_session}")
    else:
        print("current session: none")
    return 0
