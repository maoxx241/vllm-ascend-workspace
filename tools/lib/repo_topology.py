from __future__ import annotations

import configparser
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

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


def _declared_submodule_paths(root: Path) -> list[Path] | None:
    gitmodules = root / ".gitmodules"
    if not gitmodules.is_file():
        return None

    parser = configparser.ConfigParser()
    try:
        parser.read(gitmodules, encoding="utf-8")
    except configparser.Error:
        return []

    paths: list[Path] = []
    for section in parser.sections():
        if not section.startswith("submodule "):
            continue
        path_value = parser.get(section, "path", fallback="").strip()
        if path_value:
            paths.append(Path(path_value))
    return paths


def _missing_submodules(root: Path, prefix: Path | None = None) -> list[str]:
    declared = _declared_submodule_paths(root)
    if declared is None:
        if prefix is None:
            return [".gitmodules"]
        return []

    missing: list[str] = []
    for relative_path in declared:
        full_path = root / relative_path
        display_path = relative_path if prefix is None else prefix / relative_path
        if not full_path.exists() or not (full_path / ".git").exists():
            missing.append(str(display_path))
            continue
        missing.extend(_missing_submodules(full_path, display_path))
    return missing


def probe_repo_topology(paths: RepoPaths) -> Dict[str, Any]:
    remotes = _workspace_remote_urls(paths)
    if _is_placeholder_remote(remotes.get("origin")) or _is_placeholder_remote(remotes.get("upstream")):
        return {
            "status": "needs_repair",
            "detail": "workspace root remotes are placeholder or incomplete",
            "observed_at": _observed_at(),
            "evidence_source": "workspace-init",
            "remotes": remotes,
        }
    return {
        "status": "ready",
        "detail": "workspace root remotes verified",
        "observed_at": _observed_at(),
        "evidence_source": "workspace-init",
        "remotes": remotes,
    }


def probe_submodules(paths: RepoPaths) -> Dict[str, Any]:
    missing = _missing_submodules(paths.root)
    if missing:
        return {
            "status": "needs_repair",
            "detail": f"missing initialized submodules: {', '.join(missing)}",
            "observed_at": _observed_at(),
            "evidence_source": "workspace-init",
            "missing_submodules": missing,
        }
    return {
        "status": "ready",
        "detail": "declared recursive submodules are present",
        "observed_at": _observed_at(),
        "evidence_source": "workspace-init",
        "missing_submodules": [],
    }


def ensure_repo_topology_ready(paths: RepoPaths) -> Dict[str, Any]:
    return probe_repo_topology(paths)
