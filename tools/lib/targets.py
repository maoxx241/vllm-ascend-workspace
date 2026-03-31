from .config import RepoPaths
from .remote import (
    RemoteError,
    ensure_runtime,
    resolve_server_context,
    resolve_target_context,
)
from .runtime import read_state, write_state


def ensure_target(paths: RepoPaths, target_name: str) -> int:
    print(
        "target ensure: deprecated shim; use `vaws fleet` for server inventory management"
    )
    try:
        try:
            context = resolve_server_context(paths, target_name)
            routed_via = "fleet server inventory"
        except RemoteError:
            context = resolve_target_context(paths, target_name)
            routed_via = "legacy target config"
        persisted_runtime = ensure_runtime(paths, context)
        state = read_state(paths)
    except RemoteError as exc:
        print(str(exc))
        return 1
    except RuntimeError as exc:
        print(str(exc))
        return 1

    state["current_target"] = target_name
    state["runtime"] = persisted_runtime
    try:
        write_state(paths, state)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    print(f"target ensure: routed via {routed_via}")
    print(f"target ensure: ok ({target_name})")
    return 0
