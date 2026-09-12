"""Plan supported native worktree preferences during one-time client setup.

These functions never write user configuration or launch an Agent session.
Grok only reads these preferences at user scope. Claude's WorktreeCreate hook
is not a default-mode switch. Kimi's optional SessionSetup schema is probed
with its own read-only config doctor, rather than inferred from a version.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib


def grok_native_defaults(executable: str | Path | None, project: Path) -> dict:
    """Reuse acceptance of one installed binary during explicit initialization.

    The personal build may share an upstream version number. Its installation
    receipt identifies the actual bytes tested for bare startup and resume;
    another executable or an update requires fresh evidence, not a version guess.
    """
    from vaws_local_state import shared_workspace_root
    receipt_path = shared_workspace_root(project) / ".vaws-local/client-installations/grok-native.json"
    unavailable = {"supported": False, "reason": "native-default-build-unverified"}
    if not executable or not receipt_path.is_file():
        return unavailable
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        binary = Path(executable).resolve()
        if (Path(receipt["binary"]).resolve() != binary
                or receipt.get("capabilities", {}).get("bare_worktree_default") is not True):
            return unavailable
        with binary.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != receipt["sha256"]:
            return {**unavailable, "reason": "native-default-build-changed"}
        return {"supported": True, "reason": "accepted-native-default-build",
                "receipt": str(receipt_path), "sha256": actual}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return unavailable


def _set_scalar(text: str, table: str, key: str, value: str | bool) -> str:
    """Edit a scalar in an ordinary top-level TOML table, preserving other text."""
    headers = []
    for match in re.finditer(r"(?m)^[ \t]*\[.+\][ \t]*(?:#[^\n]*)?$", text):
        try:
            tomllib.loads(text[:match.start()])
        except tomllib.TOMLDecodeError:
            continue
        headers.append(match)
    matches = [index for index, match in enumerate(headers)
               if re.fullmatch(r"[ \t]*\[" + re.escape(table) + r"\][ \t]*(?:#[^\n]*)?", match[0])]
    line = key + " = " + json.dumps(value) + "\n"
    if not matches:
        return text.rstrip() + "\n\n[" + table + "]\n" + line
    if len(matches) != 1:
        raise ValueError("ambiguous preference table")
    index = matches[0]
    start = headers[index].end()
    end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
    body = text[start:end]
    assignment = re.search(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[^\n]*(?:\n|$)", body)
    if assignment:
        return text[:start + assignment.start()] + line + text[start + assignment.end():]
    return text[:end].rstrip() + "\n" + line + "\n" + text[end:]


def add_native_mode(files: dict[Path, str], notes: list, client: str,
                    project: Path, *, user_home: Path | None = None,
                    capability: dict | None = None, kimi_tui_config: Path | None = None) -> None:
    """Append planned files/notes; the existing setup owner applies and backs up.

    Call only for explicitly requested one-time initialization, never from a
    per-worktree callback. Kimi SessionSetup wiring remains with its existing
    client builder; use kimi_session_setup_capability before enabling it.
    """
    if client == "claude":
        notes.append({"client": client, "action": "unsupported", "scope": "native-cli",
                      "reason": "no-native-default-worktree-setting",
                      "detail": "Claude Code 2.1.269 exposes --worktree and background isolation, "
                                "not a default worktree setting for ordinary CLI sessions. "
                                "WorktreeCreate only handles creation already requested by the client.",
                      "reference": "https://code.claude.com/docs/en/worktrees"})
        return
    accepted = bool(capability and capability.get("supported") is True)
    if client == "kimi":
        if not accepted or kimi_tui_config is None:
            return
        path = kimi_tui_config
        values = {"upgrade": {"auto_install": False}}
        reason = "preserve-native-session-extension"
        detail = ("Native automatic installation is disabled for the verified SessionSetup extension "
                  "so an official update cannot remove its supported hook event.")
    elif client == "grok":
        home = (Path(user_home).expanduser() / ".grok" if user_home is not None
                else Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))).expanduser())
        path = home / "config.toml"
        values = {"cli": {"worktree_type": "git"},
                  "hints": {"new_session_worktree_mode": "always", "fork_worktree_mode": "always"}}
        if accepted:
            values["cli"]["auto_update"] = False
        reason = "native-worktree-preferences"
        detail = ("Grok loads these preferences only from user configuration. "
                  "They affect /new and /fork in the inspected stock client. "
                  "Automatic bare startup also requires the native startup-default patch; "
                  "only an installed binary receipt with matching hash and startup/resume "
                  "acceptance can establish that capability.")
        if accepted:
            detail += (" Native automatic updates are disabled for this accepted personal build "
                       "so an official release cannot overwrite its worktree patch.")
    else:
        return
    try:
        text = files[path] if path in files else path.read_text(encoding="utf-8") if path.exists() else ""
        original = tomllib.loads(text)
        expected = copy.deepcopy(original)
        updated = text
        for table, settings in values.items():
            if not isinstance(expected.setdefault(table, {}), dict):
                raise ValueError(table + " is not a preference table")
            for key, value in settings.items():
                if expected[table].get(key) != value:
                    updated = _set_scalar(updated, table, key, value)
                    expected[table][key] = value
        if tomllib.loads(updated) != expected:
            raise ValueError("preference syntax is not an ordinary table; existing configuration preserved")
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        notes.append({"client": client, "path": str(path), "action": "preserved",
                      "reason": "native-mode-config-needs-integration", "detail": str(exc)})
        return
    if updated != text:
        files[path] = updated
    notes.append({"client": client, "path": str(path), "scope": "user",
                  "action": "planned" if updated != text else "configured",
                  "reason": reason, "settings": values, "detail": detail})


def _disabled_servers_text(text: str, values: list[str]) -> str:
    """Replace the root disabled list, retaining unrelated TOML and comments."""
    key = "disabled_mcp_servers"
    line = key + " = " + json.dumps(values) + "\n"
    original = tomllib.loads(text)
    if key not in original:
        return line + text
    for match in re.finditer(r"(?m)^[ \t]*disabled_mcp_servers[ \t]*=", text):
        for newline in re.finditer(r"\n|\Z", text[match.end():]):
            end = match.end() + newline.end()
            try:
                prefix = tomllib.loads(text[:end])
            except tomllib.TOMLDecodeError:
                continue
            if prefix.get(key) == original[key]:
                return text[:match.start()] + line + text[end:]
            break
    raise ValueError("disabled_mcp_servers syntax requires manual integration")


def add_grok_import_dedup(files: dict, notes: list, project: Path, root: Path, *, owned_server) -> None:
    """During all-client initialization, skip our duplicate Cursor imports in Grok.

    Grok merges imported providers by their exact names. Its native underscore
    providers supersede our Cursor-only launchers; other imported servers remain.
    """
    from vaws_cursor_mcp_config import KINDS, document, owned_entry

    user_dir = Path.home()
    path = Path(os.environ.get("GROK_HOME", str(user_dir / ".grok"))).expanduser() / "config.toml"
    project_path = project / ".grok/config.toml"
    try:
        text = files[path] if path in files else path.read_text(encoding="utf-8") if path.exists() else ""
        original = tomllib.loads(text)
        local_text = (files[project_path] if project_path in files else
                      project_path.read_text(encoding="utf-8") if project_path.exists() else "")
        local = tomllib.loads(local_text)
        user_servers = original.get("mcp_servers", {})
        local_servers = local.get("mcp_servers", {})
        disabled = original.get("disabled_mcp_servers", [])
        if (not isinstance(user_servers, dict) or not isinstance(local_servers, dict)
                or not isinstance(disabled, list) or not all(isinstance(name, str) for name in disabled)):
            raise ValueError("invalid Grok MCP configuration shape")
        native = {**user_servers, **local_servers}
        cursor = {**document(user_dir / ".cursor/mcp.json", files).get("mcpServers", {}),
                  **document(project / ".cursor/mcp.json", files).get("mcpServers", {})}
        duplicates = []
        for name, kind in (("remote-dev", "remote"), ("vaws-task", "task"), ("vaws-knowledge", "knowledge")):
            alias = name.replace("-", "_")
            imported, replacement = cursor.get(name), native.get(alias)
            if (name in native or alias in disabled or not isinstance(imported, dict)
                    or not owned_entry(imported, root) or imported["args"][1] != kind
                    or not isinstance(imported.get("env"), dict)
                    or imported["env"].get("VAWS_MCP_WORKSPACE") != "${workspaceFolder}"
                    or not isinstance(replacement, dict) or replacement.get("enabled") is False
                    or replacement.get("args") != next(list(args) for args, value in KINDS.items() if value == kind)
                    or not owned_server(replacement, project)):
                continue
            duplicates.append(name)
        if not duplicates:
            return
        updated_names = disabled + [name for name in duplicates if name not in disabled]
        if updated_names != disabled:
            candidate = _disabled_servers_text(text, updated_names)
            expected = {**original, "disabled_mcp_servers": updated_names}
            if tomllib.loads(candidate) != expected:
                raise ValueError("Grok disabled list edit changed unrelated settings")
            files[path] = candidate
        notes.append({"client": "grok", "path": str(path), "scope": "user",
                      "action": "planned" if updated_names != disabled else "configured",
                      "reason": "duplicate-cursor-mcp-imports", "servers": duplicates,
                      "detail": "Disable only these VAWS Cursor imports across Grok projects; "
                                "Cursor workspace variables are unavailable in Grok. "
                                "Native underscore providers and other Cursor imports remain configured."})
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        notes.append({"client": "grok", "path": str(path), "action": "preserved",
                      "reason": "grok-compat-mcp-needs-integration", "detail": str(exc)})


def kimi_session_setup_capability(executable: str | Path) -> dict:
    """Ask the actual installed parser whether the SessionSetup extension exists.

    A second unknown event must be rejected, so a CLI that ignores arbitrary
    configuration cannot appear supported. Doctor parses fixture files only;
    neither hook commands nor model requests run, and user config is untouched.
    """
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="vaws-kimi-capability-") as folder:
            directory = Path(folder)
            environment = {**os.environ, "KIMI_CODE_HOME": str(directory)}
            for event in ("SessionSetup", "VAWSUnsupportedEvent"):
                path = directory / (event + ".toml")
                path.write_text('[[hooks]]\nevent = ' + json.dumps(event)
                                + '\ncommand = "exit 0"\n', encoding="utf-8")
                result = subprocess.run([str(executable), "doctor", "config", str(path)],
                                        cwd=directory, env=environment, capture_output=True,
                                        text=True, encoding="utf-8", errors="replace", timeout=10)
                checks.append({"event": event, "returncode": result.returncode,
                               "output": (result.stdout + result.stderr)[-2048:]})
        # The negative fixture's error must explicitly identify its hook field;
        # an unrelated failure or an unsupported doctor command is not evidence.
        supported = (checks[0]["returncode"] == 0 and checks[1]["returncode"] != 0
                     and "hooks[0].event" in checks[1]["output"]
                     and '"SessionSetup"' in checks[1]["output"])
        return {"supported": supported,
                "reason": "native-session-setup-supported" if supported else "native-session-setup-unavailable",
                "checks": checks}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"supported": False, "reason": "native-session-setup-probe-failed",
                "error": str(exc), "checks": checks}
