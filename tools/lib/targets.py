from typing import Any, Dict

import yaml

from .config import RepoPaths
from .runtime import read_state, write_state


def _read_targets_config(paths: RepoPaths) -> Dict[str, Any]:
    targets_file = paths.local_overlay / "targets.yaml"
    try:
        loaded = yaml.safe_load(targets_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError("cannot read target config: .workspace.local/targets.yaml") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError("invalid target config: .workspace.local/targets.yaml") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise RuntimeError("invalid target config: .workspace.local/targets.yaml")
    return loaded


def ensure_target(paths: RepoPaths, target_name: str) -> int:
    try:
        config = _read_targets_config(paths)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    targets = config.get("targets")
    if not isinstance(targets, dict):
        print("invalid target config: missing 'targets' map")
        return 1

    target = targets.get(target_name)
    if not isinstance(target, dict):
        print(f"unknown target: {target_name}")
        return 1

    runtime = target.get("runtime")
    persisted_runtime = {}
    if isinstance(runtime, dict):
        persisted_runtime = dict(runtime)
    if not isinstance(persisted_runtime.get("workspace_root"), str):
        persisted_runtime["workspace_root"] = "/vllm-workspace"

    try:
        state = read_state(paths)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    state["current_target"] = target_name
    state["runtime"] = persisted_runtime
    write_state(paths, state)

    print(f"target ensure: ok ({target_name})")
    return 0
