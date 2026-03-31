from dataclasses import dataclass
import shutil
from typing import Tuple

REQUIRED_COMMANDS = ("git", "ssh", "python3")
RECOMMENDED_COMMANDS = ("gh",)


class PreflightError(RuntimeError):
    """Local control-plane dependency failure."""


@dataclass(frozen=True)
class PreflightReport:
    missing_required: Tuple[str, ...]
    missing_recommended: Tuple[str, ...]


def check_local_control_plane_deps() -> PreflightReport:
    missing_required = tuple(
        command for command in REQUIRED_COMMANDS if shutil.which(command) is None
    )
    missing_recommended = tuple(
        command for command in RECOMMENDED_COMMANDS if shutil.which(command) is None
    )
    return PreflightReport(
        missing_required=missing_required,
        missing_recommended=missing_recommended,
    )


def ensure_local_control_plane_deps() -> PreflightReport:
    report = check_local_control_plane_deps()
    if report.missing_required:
        raise PreflightError(
            "missing local control-plane dependencies: "
            + ", ".join(report.missing_required)
        )
    return report
