from .config import RepoPaths
from .overlay import ensure_overlay_layout
from .workspace_diagnostics import diagnose_workspace


def doctor(paths: RepoPaths) -> int:
    result = diagnose_workspace(paths)
    if result["status"] != "ready":
        print("\n".join(str(item) for item in result["observations"]))
        return 1

    print("doctor: ok")
    return 0


def init(paths: RepoPaths) -> int:
    if paths.local_overlay.exists() and not paths.local_overlay.is_dir():
        print("invalid local overlay: .workspace.local/ exists but is not a directory")
        return 1

    ensure_overlay_layout(paths)

    print("init: ok")
    return 0
