"""Install stable Codex user hooks without changing native hook trust.

Only this workspace family's generated lifecycle/summary entries participate.
The fixed user source and command remain unchanged when a new worktree selects
a different package environment. Custom hooks and inline TOML stay untouched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from vaws_claude_config import owned_entry, same_repository
from vaws_local_owner import accessible_windows_path, managed_path, managed_receipt, windows_mounted_workspace

EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "PreToolUse", "UserPromptSubmit", "Stop")


def document(path: Path, files: dict) -> dict:
    text = files.get(path)
    if text is None:
        text = path.read_text(encoding="utf-8") if path.is_file() else "{}"
    value = json.loads(text)
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ValueError(f"Codex hook configuration must contain an object of events: {path}")
    return value


def hook_kind(item: dict, root: Path, parse_command) -> str | None:
    """Recognize exact generated argv, not an arbitrary shell wrapper."""
    if not isinstance(item, dict) or item.get("type") != "command":
        return None
    try:
        argv = parse_command(item.get("command", ""))
        if len(argv) == 2 and owned_entry(accessible_windows_path(argv[1]), root, "vaws_codex_session.py"):
            return "adapter"
        if len(argv) < 6 or len(argv) % 2:
            return None
        path = Path(accessible_windows_path(argv[1]))
        if (path.name not in {"vaws_session.py", "knowledge_summary.py"} or path.parent.name != "hooks"
                or path.parent.parent.name != ".agents" or not same_repository(path.parents[2], root)):
            return None
        allowed = {"--client", "--project", "--environment-receipt", "--agent-sessions-dir",
                   "--github-identity-file", "--coordinator-state-dir"}
        options = {argv[index]: argv[index + 1] for index in range(2, len(argv), 2)}
        if (len(options) * 2 != len(argv) - 2 or set(options) - allowed or options.get("--client") != "codex"
                or not options.get("--project")
                or not same_repository(Path(accessible_windows_path(options["--project"])), root)):
            return None
        return "summary" if path.name == "knowledge_summary.py" else "session"
    except (OSError, ValueError, TypeError):
        return None


def items(groups):
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            yield from group["hooks"]


def add_codex_setup(files: dict, notes: list, project: Path, root: Path, *, shell_command,
                    parse_command, enable: bool = False, user_path: Path | None = None) -> None:
    """Adapt an already-merged project plan, after its MCP entries are prepared."""
    if windows_mounted_workspace(root):
        if enable:
            notes.append({"path": str(project), "action": "preserved",
                          "reason": "native-codex-user-hooks-require-windows-owner"})
        return
    user_path = user_path or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "hooks.json"
    user = document(user_path, files)
    shared = user.get("hooks", {})
    configured = any(hook_kind(item, root, parse_command) == "adapter"
                     for event in EVENTS for item in items(shared.get(event, [])))
    if not enable and not configured:
        return
    local_path = project / ".codex/hooks.json"
    local = document(local_path, files)
    events = local.get("hooks", {})
    # Retain a previously reviewed bootstrap command. It uses stdlib only and
    # the immutable interpreter remains usable after package selection changes.
    command = next((item["command"] for event in EVENTS for item in items(shared.get(event, []))
                    if hook_kind(item, root, parse_command) == "adapter"), None)
    if command is None:
        entry = managed_path(root / ".agents/scripts/vaws_codex_session.py", windows=windows_mounted_workspace(root))
        command = shell_command([managed_receipt(root)["python"], entry])
    global_changed = False
    local_changed = False
    for event in EVENTS:
        groups = events.get(event, [])
        covered = any(hook_kind(item, root, parse_command) == "adapter" for item in items(shared.get(event, [])))
        wanted = any(hook_kind(item, root, parse_command) in {"session", "summary", "adapter"}
                     for item in items(groups))
        # Earlier explicit user configuration may contain the same generated
        # direct hook. Replace those entries alongside the project migration.
        kept_shared = []
        for group in shared.get(event, []):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_shared.append(group)
                continue
            remaining = [item for item in group["hooks"]
                         if hook_kind(item, root, parse_command) not in {"session", "summary"}]
            if remaining != group["hooks"]:
                global_changed = True
                wanted = True
            if remaining == group["hooks"]:
                kept_shared.append(group)
            elif remaining:
                kept_shared.append({**group, "hooks": remaining})
        if event in shared:
            shared[event] = kept_shared
        if not covered and wanted:
            shared.setdefault(event, []).append({"hooks": [{"type": "command", "command": command,
                "timeout": 3 if event == "SessionEnd" else 5 if event == "Stop" else 12}]})
            covered = True
            global_changed = True
        if not covered:
            continue
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            remaining = [item for item in group["hooks"] if hook_kind(item, root, parse_command) is None]
            if remaining != group["hooks"]:
                local_changed = True
            if remaining == group["hooks"]:
                kept.append(group)
            elif remaining:
                kept.append({**group, "hooks": remaining})
        if kept:
            events[event] = kept
        else:
            events.pop(event, None)
    if global_changed:
        user["hooks"] = shared
        files[user_path] = json.dumps(user, ensure_ascii=False, indent=2) + "\n"
    if local_changed:
        local["hooks"] = events
        files[local_path] = json.dumps(local, ensure_ascii=False, indent=2) + "\n"
        notes.append({"path": str(user_path), "action": "configured-user-hooks",
                      "reason": "stable-codex-worktree-lifecycle", "project_entries_removed": str(local_path),
                      "trust": "native-review-required-for-new-definitions"})
