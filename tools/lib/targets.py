from typing import Any, Dict

import yaml

from .config import RepoPaths
from .runtime import read_state, write_state

REQUIRED_RUNTIME_FIELDS = ("image_ref", "container_name", "ssh_port", "bootstrap_mode")


def _read_targets_config(paths: RepoPaths) -> Dict[str, Any]:
    targets_file = paths.local_overlay / "targets.yaml"
    try:
        loaded = yaml.safe_load(targets_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError("cannot read target config: .workspace.local/targets.yaml") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError("invalid target config: .workspace.local/targets.yaml") from exc
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
    if not isinstance(runtime, dict):
        print(f"invalid target config: target '{target_name}' missing runtime map")
        return 1

    missing_runtime_fields = [name for name in REQUIRED_RUNTIME_FIELDS if name not in runtime]
    if missing_runtime_fields:
        print(
            "invalid target config: "
            f"target '{target_name}' has incomplete runtime config (missing: "
            f"{', '.join(missing_runtime_fields)})"
        )
        return 1

    persisted_runtime = dict(runtime)
    if not isinstance(persisted_runtime["image_ref"], str) or not persisted_runtime[
        "image_ref"
    ].strip():
        print(
            "invalid target config: "
            f"target '{target_name}' runtime.image_ref must be a non-empty string"
        )
        return 1
    if not isinstance(persisted_runtime["container_name"], str) or not persisted_runtime[
        "container_name"
    ].strip():
        print(
            "invalid target config: "
            f"target '{target_name}' runtime.container_name must be a non-empty string"
        )
        return 1
    if not isinstance(persisted_runtime["ssh_port"], int):
        print(
            "invalid target config: "
            f"target '{target_name}' runtime.ssh_port must be an integer"
        )
        return 1
    if not isinstance(persisted_runtime["bootstrap_mode"], str) or not persisted_runtime[
        "bootstrap_mode"
    ].strip():
        print(
            "invalid target config: "
            f"target '{target_name}' runtime.bootstrap_mode must be a non-empty string"
        )
        return 1

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
