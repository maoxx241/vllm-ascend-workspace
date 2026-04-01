import json
from typing import Any, Dict

from .config import RepoPaths
from .overlay import OVERLAY_SCHEMA_VERSION


def _normalize_state_for_write(state: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise RuntimeError("invalid runtime state: .workspace.local/state.json")

    normalized = dict(state)
    schema_version = normalized.get("schema_version")
    if schema_version is None:
        normalized["schema_version"] = OVERLAY_SCHEMA_VERSION
        return normalized

    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise RuntimeError("invalid runtime state: .workspace.local/state.json")
    if schema_version != OVERLAY_SCHEMA_VERSION:
        raise RuntimeError(
            "unsupported runtime state schema_version: "
            f"{schema_version}"
        )
    return normalized


def read_state(paths: RepoPaths) -> Dict[str, Any]:
    state_file = paths.local_state_file
    if not state_file.exists():
        return {}

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid runtime state: .workspace.local/state.json") from exc

    if not isinstance(data, dict):
        raise RuntimeError("invalid runtime state: .workspace.local/state.json")
    return data


def ensure_state_schema(paths: RepoPaths) -> Dict[str, Any]:
    return _normalize_state_for_write(read_state(paths))


def write_state(paths: RepoPaths, state: Dict[str, Any]) -> None:
    state_file = paths.local_state_file
    state_file.parent.mkdir(parents=True, exist_ok=True)
    normalized_state = _normalize_state_for_write(state)
    state_file.write_text(
        json.dumps(normalized_state, indent=2) + "\n",
        encoding="utf-8",
    )


def update_state(paths: RepoPaths, **updates: Any) -> Dict[str, Any]:
    state = read_state(paths)
    state.update(updates)
    write_state(paths, state)
    return state
