from .config import RepoPaths
from .lifecycle_state import record_foundation_status
from .preflight import check_local_control_plane_deps


def run_foundation(paths: RepoPaths) -> int:
    try:
        report = check_local_control_plane_deps()
        record_foundation_status(paths, report.status)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    print(f"foundation: {report.status}")
    return 1 if report.status == "blocked" else 0
