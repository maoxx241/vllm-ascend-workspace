from pathlib import Path

import yaml

from .config import RepoPaths
from .gitflow import default_base_ref
from .runtime import read_state, write_state


def _session_manifest_path(paths: RepoPaths, session_name: str) -> Path:
    return paths.local_overlay / "sessions" / session_name / "manifest.yaml"


def _is_valid_session_name(session_name: str) -> bool:
    if not isinstance(session_name, str):
        return False
    normalized_name = session_name.strip()
    if not normalized_name:
        return False
    if normalized_name == ".":
        return False
    if "/" in normalized_name or "\\" in normalized_name:
        return False
    if ".." in normalized_name:
        return False
    return True


def _read_state_for_session_create(paths: RepoPaths):
    if not paths.local_overlay.exists() or not paths.local_overlay.is_dir():
        print("local overlay is not initialized: run `vaws init` to bootstrap .workspace.local/")
        return None

    state_file = paths.local_overlay / "state.json"
    if not state_file.is_file():
        print(
            "local overlay is not initialized: run `vaws init` to bootstrap .workspace.local/state.json"
        )
        return None

    try:
        return read_state(paths)
    except RuntimeError as exc:
        print(str(exc))
        return None


def create_session(paths: RepoPaths, session_name: str) -> int:
    if not _is_valid_session_name(session_name):
        print("invalid session name: must not contain path separators or '..'")
        return 1

    state = _read_state_for_session_create(paths)
    if state is None:
        return 1

    runtime = state.get("runtime")
    workspace_root = "/vllm-workspace"
    if isinstance(runtime, dict):
        runtime_workspace_root = runtime.get("workspace_root")
        if isinstance(runtime_workspace_root, str) and runtime_workspace_root.strip():
            workspace_root = runtime_workspace_root

    try:
        base_ref = default_base_ref(paths)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    manifest = {
        "name": session_name,
        "workspace_root": workspace_root,
        "base_ref": base_ref,
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
    if not _is_valid_session_name(session_name):
        print("invalid session name: must not contain path separators or '..'")
        return 1

    state = _read_state_for_session_create(paths)
    if state is None:
        return 1

    manifest_path = _session_manifest_path(paths, session_name)
    if not manifest_path.is_file():
        print(f"unknown session: {session_name}")
        return 1

    state["current_session"] = session_name
    write_state(paths, state)
    print(f"session switch: ok ({session_name})")
    return 0


def status_session(paths: RepoPaths) -> int:
    state = _read_state_for_session_create(paths)
    if state is None:
        return 1

    current_session = state.get("current_session")
    if isinstance(current_session, str) and current_session.strip():
        print(f"current session: {current_session}")
    else:
        print("current session: none")
    return 0
