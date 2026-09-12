"""Reuse fixed task settings from an installed native user MCP provider."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tomllib

from vaws_claude_config import owned_entry
from vaws_local_owner import accessible_windows_path

TASK_ENV = {"VAWS_AGENT_SESSIONS_DIR", "VAWS_COORDINATOR_STATE_DIR", "VAWS_GITHUB_IDENTITY_FILE"}


def servers_at(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers", {}), dict):
        raise ValueError(f"MCP configuration must contain an object of servers: {path}")
    return value.get("mcpServers", {})


def task_settings(server: dict) -> dict[str, str]:
    return {key: value for key, value in server.get("env", {}).items()
            if key in TASK_ENV and isinstance(value, str)}


def task_env(client: str, project: Path, *, kimi_config: Path | None = None) -> dict[str, str]:
    """A project provider overrides a same-name user provider in native clients."""
    if client == "codex" and (path := project / ".codex/config.toml").is_file():
        servers = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {})
        for name in ("vaws_task", "vaws-task"):
            if isinstance(server := servers.get(name), dict):
                return task_settings(server)
    relative = {"cursor": ".cursor/mcp.json", "kimi": ".kimi-code/mcp.json"}.get(client)
    if relative and (path := project / relative).is_file():
        servers = servers_at(path)
        for name in ("vaws-task", "vaws_task"):
            if isinstance(server := servers.get(name), dict):
                return task_settings(server)
    return user_task_env(client, project, kimi_config=kimi_config)


def user_task_env(client: str, project: Path, *, kimi_config: Path | None = None) -> dict[str, str]:
    """Read configuration, never native task identity or a process environment pin.

    Temporary Kimi worktree hook files are not the active user configuration;
    fall back to the installed Kimi home when that directory has no provider.
    Only the generated task entry from this Git worktree family participates.
    """
    if client == "cursor":
        paths = [Path.home() / ".cursor/mcp.json"]
    elif client == "kimi":
        home = Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))).expanduser()
        paths = [*([kimi_config.parent / "mcp.json"] if kimi_config else []), home / "mcp.json"]
    else:
        return {}
    for path in dict.fromkeys(paths):
        if not path.is_file():
            continue
        servers = servers_at(path)
        for name in ("vaws-task", "vaws_task"):
            server = servers.get(name, {})
            if not isinstance(server, dict):
                continue
            args = server.get("args", [])
            if (not isinstance(args, list) or len(args) != 2 or args[1] != "task"
                    or not isinstance(args[0], str)
                    or not owned_entry(accessible_windows_path(args[0]), project, "vaws_native_mcp.py")):
                continue
            return task_settings(server)
    return {}
