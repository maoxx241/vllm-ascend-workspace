from .config import RepoPaths
from .remote import (
    RemoteError,
    ensure_runtime,
    resolve_server_context,
    resolve_target_context,
    verify_runtime,
)
from .runtime import read_state, write_state


def ensure_target(paths: RepoPaths, target_name: str) -> int:
    print(
        "target ensure: deprecated shim; use `vaws fleet` for server inventory management"
    )
    try:
        routed_via = "legacy target config"
        context = resolve_target_context(paths, target_name)
        try:
            server_context = resolve_server_context(paths, context.host.name)
        except RemoteError:
            server_context = None

        if server_context is not None:
            context = server_context
            routed_via = "fleet verification behavior"
            try:
                verification = verify_runtime(paths, context)
                if verification.status != "ready":
                    persisted_runtime = ensure_runtime(paths, context)
                    verification = verify_runtime(paths, context)
                persisted_runtime = verification.runtime
            except RemoteError:
                persisted_runtime = ensure_runtime(paths, context)
                persisted_runtime = verify_runtime(paths, context).runtime
        else:
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
