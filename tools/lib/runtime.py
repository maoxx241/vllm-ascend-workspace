import json
from typing import Any, Dict

from .config import RepoPaths


def read_state(paths: RepoPaths) -> Dict[str, Any]:
    state_file = paths.local_overlay / "state.json"
    if not state_file.exists():
        return {}

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid runtime state: .workspace.local/state.json") from exc

    if not isinstance(data, dict):
        raise RuntimeError("invalid runtime state: .workspace.local/state.json")
    return data


def write_state(paths: RepoPaths, state: Dict[str, Any]) -> None:
    state_file = paths.local_overlay / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
