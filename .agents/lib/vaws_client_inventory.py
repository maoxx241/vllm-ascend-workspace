"""Find installed native clients without treating generated config as installation."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import shutil
import sys

CLIENT_COMMANDS = {
    "codex": ("codex",), "cursor": ("cursor-agent", "cursor"),
    "claude": ("claude",), "grok": ("grok",), "kimi": ("kimi",),
}
MAC_APPS = {
    "codex": (("Codex.app", "com.openai.codex"), ("ChatGPT.app", "com.openai.codex")),
    "cursor": (("Cursor.app", "com.todesktop.230313mzl4w4u92"),),
    "claude": (("Claude.app", "com.anthropic.claudefordesktop"),),
}


def installed_clients(*, home: Path | None = None, environment: dict | None = None,
                      platform: str | None = None) -> dict:
    """Return executable/app evidence for the five supported clients, without launching them."""
    home = home or Path.home()
    environment = dict(os.environ if environment is None else environment)
    platform = platform or sys.platform
    windows = platform == "win32"
    extensions = (".exe", ".cmd", ".bat") if windows else ("",)
    known = {
        "claude": [home / ".claude/local"],
        "grok": [Path(environment.get("GROK_HOME", str(home / ".grok"))) / "bin"],
        "kimi": [Path(environment.get("KIMI_CODE_HOME", str(home / ".kimi-code"))) / "bin"],
    }
    common = [home / ".local/bin", home / "bin"]
    if windows:
        local = Path(environment.get("LOCALAPPDATA", str(home / "AppData/Local")))
        roaming = Path(environment.get("APPDATA", str(home / "AppData/Roaming")))
        common += [roaming / "npm", local / "Microsoft/WindowsApps"]
    else:
        common += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
    result = {}
    for client, commands in CLIENT_COMMANDS.items():
        evidence = []
        known_candidates = [directory / (command + extension)
                            for directory in known.get(client, []) for command in commands for extension in extensions]
        path_candidates = [Path(found) for command in commands
                           if (found := shutil.which(command, path=environment.get("PATH", "")))]
        # Kimi's existing launcher explicitly selects its configured home first.
        # Ordinary client commands, including Grok, follow the shell's PATH.
        candidates = (known_candidates + path_candidates if client == "kimi"
                      else path_candidates + known_candidates)
        candidates += [directory / (command + extension)
                       for directory in common for command in commands for extension in extensions]
        if windows and client in {"codex", "cursor", "claude"}:
            label = {"codex": "Codex", "cursor": "Cursor", "claude": "Claude"}[client]
            candidates += [local / "Programs" / label / (label + ".exe")]
            if environment.get("ProgramFiles"):
                candidates += [Path(environment["ProgramFiles"]) / label / (label + ".exe")]
        executable = next((str(path) for path in candidates
                           if path.is_file() and (windows or os.access(path, os.X_OK))), None)
        if executable:
            evidence.append({"kind": "executable", "path": executable})
        if platform == "darwin":
            for parent in (Path("/Applications"), home / "Applications"):
                for name, bundle_id in MAC_APPS.get(client, ()):
                    path = parent / name
                    try:
                        metadata = plistlib.loads((path / "Contents/Info.plist").read_bytes())
                        binary = path / "Contents/MacOS" / metadata["CFBundleExecutable"]
                        if metadata.get("CFBundleIdentifier") == bundle_id and binary.is_file():
                            evidence.append({"kind": "desktop-app", "path": str(path)})
                    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
                        continue
        result[client] = {"installed": bool(evidence), "executable": executable, "evidence": evidence}
    return result
