from .config import RepoPaths

OVERLAY_FILES = ("targets.yaml", "repos.yaml", "auth.yaml", "state.json")


def doctor(paths: RepoPaths) -> int:
    if not paths.local_overlay.exists():
        print("missing local overlay: .workspace.local/")
        return 1

    print("doctor: ok")
    return 0


def init(paths: RepoPaths) -> int:
    paths.local_overlay.mkdir(parents=True, exist_ok=True)
    for name in OVERLAY_FILES:
        file_path = paths.local_overlay / name
        if not file_path.exists():
            file_path.write_text("", encoding="utf-8")

    print("init: ok")
    return 0
