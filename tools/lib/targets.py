from .config import RepoPaths
from .lifecycle_state import LEGACY_TARGET_HANDOFF_KIND, record_runtime_handoff
from .remote import (
    RemoteError,
    ensure_runtime,
    resolve_target_context,
    verify_runtime,
)


def ensure_target(paths: RepoPaths, target_name: str) -> int:
    print(
        "target ensure: deprecated compatibility shim; use `vaws fleet add <server>` for server handoff"
    )
    try:
        context = resolve_target_context(paths, target_name)
        ensure_runtime(paths, context)
        verification = verify_runtime(paths, context)
        record_runtime_handoff(
            paths,
            current_target=target_name,
            handoff_kind=LEGACY_TARGET_HANDOFF_KIND,
            runtime=verification.runtime,
        )
    except RemoteError as exc:
        print(str(exc))
        return 1
    except RuntimeError as exc:
        print(str(exc))
        return 1

    print(f"target ensure: ok ({target_name})")
    return 0
