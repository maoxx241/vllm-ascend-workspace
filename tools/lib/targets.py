from .config import RepoPaths
from .remote import RemoteError, ensure_runtime, resolve_target_context
from .runtime import read_state, write_state


def ensure_target(paths: RepoPaths, target_name: str) -> int:
    try:
        context = resolve_target_context(paths, target_name)
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
    write_state(paths, state)
    print(f"target ensure: ok ({target_name})")
    return 0
