from .config import RepoPaths

OVERLAY_FILES = ("targets.yaml", "repos.yaml", "auth.yaml", "state.json")


def doctor(paths: RepoPaths) -> int:
    if not paths.local_overlay.exists():
        print("missing local overlay: .workspace.local/")
        return 1
    if not paths.local_overlay.is_dir():
        print("invalid local overlay: .workspace.local/ must be a directory")
        return 1

    missing_files = [
        name for name in OVERLAY_FILES if not (paths.local_overlay / name).is_file()
    ]
    if missing_files:
        print(f"missing overlay files: {', '.join(missing_files)}")
        return 1

    print("doctor: ok")
    return 0


def init(paths: RepoPaths) -> int:
    if paths.local_overlay.exists() and not paths.local_overlay.is_dir():
        print("invalid local overlay: .workspace.local/ exists but is not a directory")
        return 1

    paths.local_overlay.mkdir(parents=True, exist_ok=True)
    for name in OVERLAY_FILES:
        file_path = paths.local_overlay / name
        if not file_path.exists():
            if name == "state.json":
                file_path.write_text("{}\n", encoding="utf-8")
            else:
                file_path.write_text("", encoding="utf-8")

    print("init: ok")
    return 0
