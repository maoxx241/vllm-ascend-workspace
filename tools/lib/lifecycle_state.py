from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Tuple

from .config import RepoPaths
from .runtime import read_state, write_state

MANAGED_SERVER_HANDOFF_KIND = "managed_server"
LEGACY_TARGET_HANDOFF_KIND = "legacy_target"


def _load_state_with_lifecycle(paths: RepoPaths) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    state = read_state(paths)
    lifecycle = state.get("lifecycle")
    if not isinstance(lifecycle, dict):
        lifecycle = {}
        state["lifecycle"] = lifecycle
    return state, lifecycle


def get_lifecycle_state(paths: RepoPaths) -> Dict[str, Any]:
    state = read_state(paths)
    lifecycle = state.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return {}
    return lifecycle


def record_requested_mode(paths: RepoPaths, requested_mode: str) -> None:
    state, lifecycle = _load_state_with_lifecycle(paths)
    lifecycle["requested_mode"] = requested_mode
    write_state(paths, state)


def _record_profile_status(paths: RepoPaths, profile_name: str, status: str) -> None:
    state, lifecycle = _load_state_with_lifecycle(paths)
    profile = lifecycle.get(profile_name)
    if not isinstance(profile, dict):
        profile = {}
        lifecycle[profile_name] = profile
    profile["status"] = status
    write_state(paths, state)


def record_foundation_status(paths: RepoPaths, status: str) -> None:
    _record_profile_status(paths, "foundation", status)


def record_git_profile_status(paths: RepoPaths, status: str) -> None:
    _record_profile_status(paths, "git_profile", status)


def record_runtime_handoff(
    paths: RepoPaths,
    *,
    current_target: str,
    handoff_kind: str,
    runtime: Dict[str, Any],
) -> None:
    state, _lifecycle = _load_state_with_lifecycle(paths)
    state["current_target"] = current_target
    state["current_target_kind"] = handoff_kind
    state["runtime"] = deepcopy(runtime)
    state.pop("current_session", None)
    write_state(paths, state)


def _runtime_matches_context(runtime: Dict[str, Any], context: Any) -> bool:
    context_runtime = getattr(context, "runtime", None)
    context_host = getattr(context, "host", None)
    if context_runtime is None or context_host is None:
        return False

    expected = {
        "container_name": context_runtime.container_name,
        "ssh_port": context_runtime.ssh_port,
        "workspace_root": context_runtime.workspace_root,
        "bootstrap_mode": context_runtime.bootstrap_mode,
        "host_name": context_host.name,
        "host": context_host.host,
        "host_port": context_host.port,
        "login_user": context_host.login_user,
    }
    for key, expected_value in expected.items():
        actual_value = runtime.get(key)
        if actual_value is not None and actual_value != expected_value:
            return False
    return True


def infer_current_target_kind(
    paths: RepoPaths,
    current_target: str,
    runtime: Any,
):
    from .remote import RemoteError, resolve_server_context, resolve_target_context

    candidates = []
    for handoff_kind, resolver in (
        (MANAGED_SERVER_HANDOFF_KIND, resolve_server_context),
        (LEGACY_TARGET_HANDOFF_KIND, resolve_target_context),
    ):
        try:
            context = resolver(paths, current_target)
        except RemoteError:
            continue
        candidates.append((handoff_kind, context))

    if not candidates:
        return None

    if isinstance(runtime, dict):
        matching = [
            handoff_kind
            for handoff_kind, context in candidates
            if _runtime_matches_context(runtime, context)
        ]
        if len(matching) == 1:
            return matching[0]
        return None

    if len(candidates) == 1:
        return candidates[0][0]
    return None
