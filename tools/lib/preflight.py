import shutil
import sys
from dataclasses import dataclass
from typing import Tuple

REQUIRED_COMMANDS = ("git", "ssh", "python3")
RECOMMENDED_COMMANDS = ()
PROVIDER_REQUIRED_COMMANDS = {
    "github-cli": ("gh",),
}


class PreflightError(RuntimeError):
    """Local control-plane dependency failure."""


@dataclass(frozen=True)
class PreflightReport:
    status: str
    installed_required: Tuple[str, ...]
    missing_required: Tuple[str, ...]
    installed_recommended: Tuple[str, ...]
    missing_recommended: Tuple[str, ...]


def _python3_is_available() -> bool:
    if shutil.which("python3") is not None:
        return True
    return bool(sys.executable)


def check_local_control_plane_deps(*, git_provider: str = "github-cli") -> PreflightReport:
    required_commands = REQUIRED_COMMANDS + PROVIDER_REQUIRED_COMMANDS.get(git_provider, ())
    installed_required = []
    missing_required = []
    for command in required_commands:
        if command == "python3":
            if _python3_is_available():
                installed_required.append(command)
            else:
                missing_required.append(command)
            continue
        if shutil.which(command) is not None:
            installed_required.append(command)
        else:
            missing_required.append(command)

    installed_recommended = []
    missing_recommended = []
    for command in RECOMMENDED_COMMANDS:
        if shutil.which(command) is not None:
            installed_recommended.append(command)
        else:
            missing_recommended.append(command)

    if missing_required:
        status = "blocked"
    elif missing_recommended:
        status = "degraded"
    else:
        status = "ready"

    return PreflightReport(
        status=status,
        installed_required=tuple(installed_required),
        missing_required=tuple(missing_required),
        installed_recommended=tuple(installed_recommended),
        missing_recommended=tuple(missing_recommended),
    )


def ensure_local_control_plane_deps() -> PreflightReport:
    report = check_local_control_plane_deps()
    if report.status == "blocked":
        raise PreflightError(
            f"missing local control-plane dependencies: {', '.join(report.missing_required)}"
        )
    return report
