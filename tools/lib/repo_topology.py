from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any, Dict

from .capability_state import read_capability_state, write_capability_state
from .config import RepoPaths


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(repo_path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return result.stdout.strip()


def _safe_remote_url(repo_path, remote_name: str) -> str | None:
    try:
        remote_url = _git(repo_path, "remote", "get-url", remote_name)
    except RuntimeError:
        return None
    return remote_url or None


def _workspace_remote_urls(paths: RepoPaths) -> Dict[str, str | None]:
    return {
        "origin": _safe_remote_url(paths.root, "origin"),
        "upstream": _safe_remote_url(paths.root, "upstream"),
    }


def _is_placeholder_remote(url: Any) -> bool:
    if not isinstance(url, str) or not url.strip():
        return True
    normalized = url.strip().lower()
    return "your-org" in normalized or "example/" in normalized


def ensure_repo_topology_ready(paths: RepoPaths) -> Dict[str, Any]:
    remotes = _workspace_remote_urls(paths)
    if _is_placeholder_remote(remotes.get("origin")) or _is_placeholder_remote(remotes.get("upstream")):
        payload = {
            "status": "needs_repair",
            "detail": "workspace root remotes are placeholder or incomplete",
            "observed_at": _observed_at(),
            "evidence_source": "workspace-init",
        }
    else:
        payload = {
            "status": "ready",
            "detail": "workspace root and submodule remotes verified",
            "observed_at": _observed_at(),
            "evidence_source": "workspace-init",
        }
    state = read_capability_state(paths)
    state["repo_topology"] = payload
    write_capability_state(paths, state)
    return payload
