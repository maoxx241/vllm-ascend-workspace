"""Add native worktree setup commands without taking over client preferences.

This only plans configuration files. The native client owns worktree creation
and runs the command before handing its new editing directory to the agent.
"""
from __future__ import annotations

import copy
import json
import re
import tomllib
from pathlib import Path


def _note(notes: list, path: Path, reason: str, detail: str = "") -> None:
    notes.append({"path": str(path), "action": "preserved", "reason": reason,
                  **({"detail": detail} if detail else {})})


def _read(files: dict[Path, str], path: Path) -> str:
    return files[path] if path in files else path.read_text(encoding="utf-8")


def _prepend(script: str, command: str) -> str:
    # Choose code and pinned components before user commands build against it.
    # Remove earlier copies while retaining every user line and its order.
    user_script = "".join(line for line in script.splitlines(keepends=True) if line.strip() != command)
    return command + ("\n" + user_script if user_script else "")


def _toml_script(text: str, table: str, script: str) -> str:
    """Edit one ordinary table, preserving other TOML text and values.

    Parsing the prefix distinguishes actual headers from header-looking text
    inside multiline strings. Unsupported inline tables stay with their owner.
    """
    headers = []
    for match in re.finditer(r"(?m)^\s*\[([^\]\n]+)\]\s*(?:#[^\n]*)?$", text):
        try:
            tomllib.loads(text[:match.start()])
        except tomllib.TOMLDecodeError:
            continue
        headers.append(match)
    matching = [i for i, match in enumerate(headers) if match[1].strip() == table]
    rendered = "script = " + json.dumps(script, ensure_ascii=False) + "\n"
    if not matching:
        return text.rstrip() + f"\n\n[{table}]\n" + rendered
    if len(matching) != 1:
        raise ValueError("ambiguous setup table")
    index = matching[0]
    start = headers[index].end()
    end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
    body = text[start:end]
    assignment = re.search(r"(?m)^[ \t]*script[ \t]*=", body)
    if assignment is None:
        return text[:end].rstrip() + "\n" + rendered + "\n" + text[end:]
    value_start = start + assignment.end()
    # A value may be a multiline TOML string. Stop at its first complete line.
    for boundary in [match.end() for match in re.finditer(r"\n|$", text[value_start:end])]:
        value_end = value_start + boundary
        try:
            value = tomllib.loads("value = " + text[value_start:value_end])["value"]
        except (KeyError, tomllib.TOMLDecodeError):
            continue
        if isinstance(value, str):
            return text[:start + assignment.start()] + rendered + text[value_end:]
    raise ValueError("unsupported setup script syntax")


def _codex(files: dict[Path, str], notes: list, project: Path, command: str) -> None:
    directory = project / ".codex/environments"
    candidates = set(directory.glob("*.toml")) if directory.is_dir() else set()
    candidates.update(path for path in files if path.parent == directory and path.suffix == ".toml")
    if len(candidates) > 1:
        _note(notes, directory, "multiple-native-environments-selection-unknown")
        return
    path = next(iter(candidates), directory / "environment.toml")
    if not candidates:
        files[path] = 'version = 1\nname = "VAWS"\n\n[setup]\nscript = ' + json.dumps(command) + "\n"
        return
    try:
        original = _read(files, path)
        document = tomllib.loads(original)
        if type(document.get("version")) is not int or document["version"] < 1:
            raise ValueError("unsupported environment version")
        if not isinstance(document.get("name"), str):
            raise ValueError("environment name is not a string")
        setup = document.get("setup", {})
        if not isinstance(setup, dict) or not isinstance(setup.get("script", ""), str):
            raise ValueError("unsupported setup")
        expected = copy.deepcopy(document)
        expected.setdefault("setup", {})
        updated = original
        for platform in (None, "darwin", "linux", "win32"):
            if platform is None:
                block, target, table = setup, expected["setup"], "setup"
            else:
                if platform not in setup:
                    continue
                block = setup[platform]
                if not isinstance(block, dict) or not isinstance(block.get("script", ""), str):
                    raise ValueError("unsupported platform setup")
                target, table = expected["setup"][platform], "setup." + platform
            target["script"] = _prepend(block.get("script", ""), command)
            if target["script"] != block.get("script"):
                updated = _toml_script(updated, table, target["script"])
        if tomllib.loads(updated) != expected:
            raise ValueError("unsupported environment syntax")
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        _note(notes, path, "native-environment-setup-needs-integration", str(exc))
        return
    if updated != original:
        files[path] = updated


def _cursor(files: dict[Path, str], notes: list, project: Path, command: str) -> None:
    path = project / ".cursor/worktrees.json"
    try:
        document = json.loads(_read(files, path)) if path in files or path.exists() else {}
        if not isinstance(document, dict):
            raise ValueError("unsupported worktree configuration")
        keys = ("setup-worktree", "setup-worktree-unix", "setup-worktree-windows")
        for key in keys:
            value = document.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                # A script path is valid client configuration, but changing it
                # into a shell command would change its interpreter/path rules.
                raise ValueError(key + " is not an appendable command array")
        updated = copy.deepcopy(document)
        for key in keys:
            if key != "setup-worktree" and key not in updated:
                continue
            commands = updated.setdefault(key, [])
            updated[key] = [command, *(item for item in commands if item.strip() != command)]
    except (OSError, UnicodeError, ValueError) as exc:
        _note(notes, path, "native-worktree-setup-needs-integration", str(exc))
        return
    if updated != document:
        files[path] = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"


def add_native_setup(files: dict[Path, str], notes: list, client: str,
                     project: Path, root: Path) -> None:
    """Plan a client-owned setup callback; never run it or change app defaults."""
    if client not in {"codex", "cursor"}:
        return
    # The new checkout can predate this callback. Load the installed source
    # entry while keeping the native new cwd. Python reads the path verbatim,
    # so POSIX shells, cmd and PowerShell never interpolate a checkout path.
    source_key = "CODEX_SOURCE_TREE_PATH" if client == "codex" else "ROOT_WORKTREE_PATH"
    bootstrap = ("import os,runpy;runpy.run_path(os.path.join(os.environ['" + source_key
                 + "'],'.agents/scripts/vaws_worktree_setup.py'),run_name='__main__')")
    command = 'uv run --no-project python -c "' + bootstrap + '" --client ' + client
    {"codex": _codex, "cursor": _cursor}[client](files, notes, project, command)
