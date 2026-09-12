"""Install generated Kimi providers once in native user MCP configuration."""
from __future__ import annotations

import json
import hashlib
import re
import tomllib
from pathlib import Path

from vaws_claude_config import KINDS, owned_entry, same_repository
from vaws_environment import PIN_ENV
from vaws_local_owner import managed_path, managed_receipt, windows_mounted_workspace

DERIVED_ENV = {PIN_ENV, "REMOTE_DEV_STATE_DIR", "VAWS_KNOWLEDGE_CONFIG",
               "VAWS_KNOWLEDGE_PROJECT_ROOTS", "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE"}


def _hook_kind(hook, project, root, parse_command):
    events = {"SessionSetup", "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
              "PreToolUse", "UserPromptSubmit"}
    if not isinstance(hook, dict) or set(hook) - {"event", "command", "timeout"} or hook.get("event") not in events:
        return None
    try:
        argv = parse_command(hook.get("command", ""))
        if (len(argv) == 7 and argv[:4] == ["uv", "run", "--no-project", "python"]
                and owned_entry(argv[4], root, "vaws_kimi_session_setup.py") and argv[5] == "--project"
                and same_repository(Path(argv[6]), project)):
            return "adapter"
        if len(argv) < 6:
            return None
        entry = Path(argv[1])
        if not (entry.name == "vaws_session.py" and entry.parent.name == "hooks"
                and entry.parent.parent.name == ".agents" and same_repository(entry.parents[2], root)):
            return None
        options = argv[2:]
        allowed = {"--client", "--project", "--agent-sessions-dir", "--github-identity-file",
                   "--coordinator-state-dir", "--environment-receipt"}
        if len(options) % 2:
            return None
        values = {}
        for index in range(0, len(options), 2):
            key = options[index]
            if key not in allowed or key in values:
                return None
            values[key] = options[index + 1]
        if values.get("--client") == "kimi" and values.get("--project") and same_repository(Path(values["--project"]), project):
            return "direct"
    except (ValueError, OSError, TypeError):
        pass
    return None


def kimi_session_setup_enabled(text: str, project: Path, root: Path, *, parse_command) -> bool:
    return any(hook.get("event") == "SessionSetup" and _hook_kind(hook, project, root, parse_command) == "adapter"
               for hook in tomllib.loads(text).get("hooks", []))


def migrate_kimi_hooks(text: str, project: Path, root: Path, *, parse_command) -> str:
    """Replace obsolete generated callbacks covered by this family adapter."""
    current = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    marker = re.compile(r"^# BEGIN VAWS session-([0-9a-f]{16})\n(.*?)^# END VAWS session-\1(?:\n|$)", re.M | re.S)

    def owned(hook):
        return _hook_kind(hook, project, root, parse_command) is not None

    def obsolete(match):
        if match[1] == current:
            return match[0]
        try:
            parsed = tomllib.loads(match[2])
        except tomllib.TOMLDecodeError:
            return match[0]
        hooks = parsed.get("hooks", [])
        return "" if set(parsed) == {"hooks"} and hooks and all(owned(hook) for hook in hooks) else match[0]

    text = marker.sub(obsolete, text)
    table = re.compile(r"^\[\[hooks\]\][ \t]*\n.*?(?=^[ \t]*\[|^# BEGIN VAWS|^# END VAWS|\Z)", re.M | re.S)

    def old_unmarked(match):
        try:
            parsed = tomllib.loads(match[0])
        except tomllib.TOMLDecodeError:
            return match[0]
        hooks = parsed.get("hooks", [])
        return "" if set(parsed) == {"hooks"} and len(hooks) == 1 and owned(hooks[0]) else match[0]

    # Native configuration writers can remove generated comments. Only exact
    # generated hook entries outside remaining managed blocks are eligible.
    result, end = [], 0
    for match in marker.finditer(text):
        result.extend((table.sub(old_unmarked, text[end:match.start()]), match[0]))
        end = match.end()
    result.append(table.sub(old_unmarked, text[end:]))
    return "".join(result)


def add_kimi_user_mcp(files: dict, notes: list, project: Path, root: Path, home: Path, *, owned_server) -> None:
    project_path, user_path = project / ".kimi-code/mcp.json", home / "mcp.json"
    local = json.loads(files.get(project_path, "{}"))
    existing = files.get(user_path)
    if existing is None:
        existing = user_path.read_text(encoding="utf-8") if user_path.exists() else "{}"
    user = json.loads(existing)
    user_servers = user.setdefault("mcpServers", {})
    entry = managed_path(root / ".agents/scripts/vaws_native_mcp.py", windows=windows_mounted_workspace(root))
    bootstrap = managed_receipt(root)["python"]
    for name, server in list(local.get("mcpServers", {}).items()):
        kind = KINDS.get(tuple(server.get("args", [])))
        if kind is None or not owned_server(server, project):
            continue
        prior = user_servers.get(name)
        if prior is not None:
            args = prior.get("args", [])
            wrapped = (len(args) == 2 and args[1] == kind
                       and owned_entry(args[0], root, "vaws_native_mcp.py"))
            if not wrapped and not owned_server(prior, project):
                notes.append({"path": str(user_path), "server": name, "action": "preserved",
                              "reason": "custom-user-provider"})
                continue
        wanted = {**server, **(prior or {}), "command": bootstrap, "args": [entry, kind]}
        wanted["env"] = {key: value for key, value in {**server.get("env", {}), **(prior or {}).get("env", {})}.items()
                         if key not in DERIVED_ENV}
        user_servers[name] = wanted
        del local["mcpServers"][name]
        notes.append({"path": str(user_path), "server": name, "action": "updated-managed",
                      "reason": "native-user-mcp-saved-workspace-environment"})
    files[user_path] = json.dumps(user, ensure_ascii=False, indent=2) + "\n"
    files[project_path] = json.dumps(local, ensure_ascii=False, indent=2) + "\n"
