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


def managed_python(repo_root: Path) -> str:
    """Reuse the prepared native owner; never infer it from an old venv path."""
    from vaws_environment import native_ready, windows_ready
    if windows_mounted_workspace(repo_root):
        receipt = windows_ready(repo_root)
        return accessible_windows_path(receipt["python"])
    return str(native_ready(repo_root)["python"])


def managed_receipt(repo_root: Path) -> dict:
    from vaws_environment import native_ready, windows_ready
    return windows_ready(repo_root) if windows_mounted_workspace(repo_root) else native_ready(repo_root)


def accessible_windows_path(value: object) -> str:
    text = str(value)
    match = re.fullmatch(r"([a-zA-Z]):[\\/](.*)", text)
    if os.name != "nt" and os.environ.get("WSL_DISTRO_NAME") and match:
        return "/mnt/" + match[1].lower() + "/" + match[2].replace("\\", "/")
    return text


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
