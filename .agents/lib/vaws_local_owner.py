"""Native paths and environment for one process owner in a mounted workspace."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping


def windows_mounted_workspace(repo_root: Path) -> bool:
    return bool(
        os.name != "nt"
        and os.environ.get("WSL_DISTRO_NAME")
        and re.fullmatch(r"/mnt/[a-zA-Z](?:/.*)?", str(repo_root))
    )


def managed_python(
    repo_root: Path, *, interpreter: str | None = None, require_windows: bool = False,
) -> str:
    """Reuse the existing native owner; knowledge never falls back to a second DB."""
    candidate = repo_root / ".vaws-local/venvs/win32/Scripts/python.exe"
    if require_windows:
        if windows_mounted_workspace(repo_root):
            return str(candidate)
    elif os.name != "nt" and os.environ.get("WSL_DISTRO_NAME") and candidate.is_file():
        return str(candidate)
    return interpreter or sys.executable


def managed_path(value: object, *, windows: bool) -> str:
    """Arguments to a Windows child use the mounted drive's native spelling."""
    if not windows or re.fullmatch(r"[a-zA-Z]:[\\/].*", str(value)):
        return str(value)
    match = re.fullmatch(r"/mnt/([a-zA-Z])(?:/(.*))?", str(value))
    if not match:
        raise ValueError("Windows-backed WSL paths must be on a mounted Windows drive")
    return match[1].upper() + ":\\" + (match[2] or "").replace("/", "\\")


def windows_interop_env(environment: Mapping[str, str]) -> dict[str, str]:
    """Forward explicit MCP env to a Windows child started through WSL."""
    result = dict(environment)
    entries = [part for part in result.get("WSLENV", "").split(":") if part]
    present = {part.split("/", 1)[0] for part in entries}
    entries.extend(key + "/w" for key in sorted(result) if key != "WSLENV" and key not in present)
    if entries:
        result["WSLENV"] = ":".join(entries)
    return result
