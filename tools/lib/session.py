from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from .config import RepoPaths
from .gitflow import base_ref_for_repo
from .remote import RemoteError, create_remote_session, resolve_server_context, switch_remote_session
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


def _read_state_for_session(paths: RepoPaths):
    if not paths.local_overlay.exists() or not paths.local_overlay.is_dir():
        print("local overlay is not initialized: run `vaws init` first")
        return None

    state_file = paths.local_overlay / "state.json"
    if not state_file.is_file():
        print("local overlay is not initialized: run `vaws init` first")
        return None

    try:
        return read_state(paths)
    except RuntimeError as exc:
        print(str(exc))
        return None


def _current_server_from_state(state: Dict[str, Any]) -> str:
    current_server = state.get("current_server")
    if not isinstance(current_server, str) or not current_server.strip():
        raise RemoteError(
            "no current server: run `vaws machine add <server>` or `vaws machine verify <server>` first"
        )
    return current_server.strip()


def _require_ready_container_access(state: Dict[str, Any], server_name: str) -> None:
    servers = state.get("servers")
    if not isinstance(servers, dict):
        raise RemoteError(
            f"server '{server_name}' is not ready: run `vaws machine verify {server_name}` first"
        )
    server_state = servers.get(server_name)
    if not isinstance(server_state, dict):
        raise RemoteError(
            f"server '{server_name}' is not ready: run `vaws machine verify {server_name}` first"
        )
    container_access = server_state.get("container_access")
    if not isinstance(container_access, dict) or container_access.get("status") != "ready":
        raise RemoteError(
            f"server '{server_name}' is not ready: run `vaws machine verify {server_name}` first"
        )


def _session_transport(mode: str) -> str:
    if mode == "local-simulation":
        return "simulation"
    return "container-ssh"


def _build_manifest(
    paths: RepoPaths,
    session_name: str,
    server_name: str,
    workspace_root: str,
) -> Dict[str, Any]:
    branch_name = f"feature/{session_name}"
    return {
        "name": session_name,
        "target": server_name,
        "workspace_ref": {
            "branch": branch_name,
            "base_ref": base_ref_for_repo(paths, "workspace"),
        },
        "vllm_ref": {
            "branch": branch_name,
            "base_ref": base_ref_for_repo(paths, "vllm"),
        },
        "vllm_ascend_ref": {
            "branch": branch_name,
            "base_ref": base_ref_for_repo(paths, "vllm-ascend"),
        },
        "stack_lock": {
            "python": "python3",
            "notes": [],
        },
        "runtime": {
            "venv_path": f"{workspace_root}/.vaws/sessions/{session_name}/.venv",
            "active_paths": {
                "vllm": f"{workspace_root}/.vaws/sessions/{session_name}/vllm",
                "vllm_ascend": f"{workspace_root}/.vaws/sessions/{session_name}/vllm-ascend",
            },
        },
    }


def create_session(paths: RepoPaths, session_name: str) -> int:
    if not _is_valid_session_name(session_name):
        print("invalid session name: must not contain path separators or '..'")
        return 1

    state = _read_state_for_session(paths)
    if state is None:
        return 1

    try:
        server_name = _current_server_from_state(state)
        _require_ready_container_access(state, server_name)
        context = resolve_server_context(paths, server_name)
        manifest = _build_manifest(paths, session_name, server_name, context.runtime.workspace_root)
        create_remote_session(paths, context, manifest, _session_transport(context.credential.mode))
    except RemoteError as exc:
        print(str(exc))
        return 1
    except RuntimeError as exc:
        print(str(exc))
        return 1

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

    state = _read_state_for_session(paths)
    if state is None:
        return 1

    manifest_path = _session_manifest_path(paths, session_name)
    if not manifest_path.is_file():
        print(f"unknown session: {session_name}")
        return 1

    try:
        server_name = _current_server_from_state(state)
        _require_ready_container_access(state, server_name)
        context = resolve_server_context(paths, server_name)
        switch_remote_session(context, session_name, _session_transport(context.credential.mode))
    except RemoteError as exc:
        print(str(exc))
        return 1

    state["current_session"] = session_name
    try:
        write_state(paths, state)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    print(f"session switch: ok ({session_name})")
    return 0


def status_session(paths: RepoPaths) -> int:
    state = _read_state_for_session(paths)
    if state is None:
        return 1

    current_server = state.get("current_server")
    if isinstance(current_server, str) and current_server.strip():
        print(f"current server: {current_server}")
    else:
        print("current server: none")

    current_session = state.get("current_session")
    if isinstance(current_session, str) and current_session.strip():
        print(f"current session: {current_session}")
    else:
        print("current session: none")
    return 0
