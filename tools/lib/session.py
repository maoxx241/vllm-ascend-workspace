from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from .config import RepoPaths
from .gitflow import base_ref_for_repo
from .remote import (
    RemoteError,
    create_remote_session,
    resolve_server_context,
    resolve_target_context,
    switch_remote_session,
)
from .runtime import read_state, write_state
from .lifecycle_state import (
    LEGACY_TARGET_HANDOFF_KIND,
    MANAGED_SERVER_HANDOFF_KIND,
    infer_current_target_kind,
)


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


def _build_manifest(paths: RepoPaths, session_name: str, state: Dict[str, Any]) -> Dict[str, Any]:
    current_target = _current_target_from_state(state)

    runtime = state.get("runtime")
    workspace_root = "/vllm-workspace"
    transport = "docker-exec"
    if isinstance(runtime, dict):
        runtime_workspace_root = runtime.get("workspace_root")
        if isinstance(runtime_workspace_root, str) and runtime_workspace_root.strip():
            workspace_root = runtime_workspace_root
        runtime_transport = runtime.get("transport")
        if isinstance(runtime_transport, str) and runtime_transport.strip():
            transport = runtime_transport

    branch_name = f"feature/{session_name}"
    manifest = {
        "name": session_name,
        "target": current_target,
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
        "_transport": transport,
    }
    return manifest


def _target_context_from_state(paths: RepoPaths, state: Dict[str, Any]):
    target_name = _current_target_from_state(state)
    handoff_kind = _current_target_kind_from_state(paths, state)
    if handoff_kind == MANAGED_SERVER_HANDOFF_KIND:
        return resolve_server_context(paths, target_name)
    if handoff_kind == LEGACY_TARGET_HANDOFF_KIND:
        return resolve_target_context(paths, target_name)
    raise RemoteError(
        "missing current target handoff kind: rerun `vaws fleet add <server>` or `vaws target ensure <target>`"
    )


def _current_target_from_state(state: Dict[str, Any]) -> str:
    current_target = state.get("current_target")
    if not isinstance(current_target, str) or not current_target.strip():
        raise RemoteError("no current target: run `vaws fleet add <server>` first")
    return current_target.strip()


def _current_target_kind_from_state(paths: RepoPaths, state: Dict[str, Any]) -> str:
    current_target_kind = state.get("current_target_kind")
    if not isinstance(current_target_kind, str) or not current_target_kind.strip():
        current_target = _current_target_from_state(state)
        runtime = state.get("runtime")
        inferred_kind = infer_current_target_kind(
            paths,
            current_target,
            runtime if isinstance(runtime, dict) else None,
        )
        if inferred_kind is None:
            raise RemoteError(
                "missing current target handoff kind: rerun `vaws fleet add <server>` or `vaws target ensure <target>`"
            )
        return inferred_kind
    return current_target_kind.strip()


def create_session(paths: RepoPaths, session_name: str) -> int:
    if not _is_valid_session_name(session_name):
        print("invalid session name: must not contain path separators or '..'")
        return 1

    state = _read_state_for_session(paths)
    if state is None:
        return 1

    try:
        manifest = _build_manifest(paths, session_name, state)
        context = _target_context_from_state(paths, state)
        transport = manifest.pop("_transport")
        create_remote_session(paths, context, manifest, transport)
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
        context = _target_context_from_state(paths, state)
        runtime = state.get("runtime", {})
        transport = "docker-exec"
        if isinstance(runtime, dict):
            runtime_transport = runtime.get("transport")
            if isinstance(runtime_transport, str) and runtime_transport.strip():
                transport = runtime_transport
        switch_remote_session(context, session_name, transport)
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

    current_target = state.get("current_target")
    if isinstance(current_target, str) and current_target.strip():
        print(f"current target: {current_target}")
    else:
        print("current target: none")

    current_session = state.get("current_session")
    if isinstance(current_session, str) and current_session.strip():
        print(f"current session: {current_session}")
    else:
        print("current session: none")
    return 0
