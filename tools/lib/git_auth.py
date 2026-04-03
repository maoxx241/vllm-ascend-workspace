from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any, Dict

from .config import RepoPaths


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _gh_auth_status_ok() -> bool:
    result = subprocess.run(
        ["gh", "auth", "status"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def probe_git_auth() -> Dict[str, Any]:
    if _gh_auth_status_ok():
        return {
            "status": "ready",
            "provider": "github-cli",
            "detail": "gh auth status ok",
            "observed_at": _observed_at(),
            "evidence_source": "workspace-init",
        }
    return {
        "status": "needs_input",
        "provider": "github-cli",
        "detail": "gh auth status not ready",
        "observed_at": _observed_at(),
        "evidence_source": "workspace-init",
    }


def ensure_git_auth_ready(paths: RepoPaths) -> Dict[str, Any]:
    return probe_git_auth()
