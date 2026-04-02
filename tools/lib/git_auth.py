from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any, Dict

from .capability_state import read_capability_state, write_capability_state
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


def ensure_git_auth_ready(paths: RepoPaths) -> Dict[str, Any]:
    state = read_capability_state(paths)
    if _gh_auth_status_ok():
        payload = {
            "status": "ready",
            "provider": "github-cli",
            "detail": "gh auth status ok",
            "observed_at": _observed_at(),
            "evidence_source": "workspace-init",
        }
    else:
        payload = {
            "status": "needs_input",
            "provider": "github-cli",
            "detail": "gh auth status not ready",
            "observed_at": _observed_at(),
            "evidence_source": "workspace-init",
        }
    state["git_auth"] = payload
    write_capability_state(paths, state)
    return payload
