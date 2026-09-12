"""Install generated Kimi providers once in native user MCP configuration."""
from __future__ import annotations

import json
from pathlib import Path

from vaws_claude_config import KINDS, owned_entry
from vaws_environment import PIN_ENV

DERIVED_ENV = {PIN_ENV, "REMOTE_DEV_STATE_DIR", "VAWS_KNOWLEDGE_CONFIG",
               "VAWS_KNOWLEDGE_PROJECT_ROOTS", "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE"}


def add_kimi_user_mcp(files: dict, notes: list, project: Path, root: Path, home: Path, *, owned_server) -> None:
    project_path, user_path = project / ".kimi-code/mcp.json", home / "mcp.json"
    local = json.loads(files.get(project_path, "{}"))
    existing = files.get(user_path)
    if existing is None:
        existing = user_path.read_text(encoding="utf-8") if user_path.exists() else "{}"
    user = json.loads(existing)
    user_servers = user.setdefault("mcpServers", {})
    entry = str(root / ".agents/scripts/vaws_native_mcp.py")
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
        wanted = {**server, **(prior or {}), "command": server["command"], "args": [entry, kind]}
        wanted["env"] = {key: value for key, value in {**server.get("env", {}), **(prior or {}).get("env", {})}.items()
                         if key not in DERIVED_ENV}
        user_servers[name] = wanted
        del local["mcpServers"][name]
        notes.append({"path": str(user_path), "server": name, "action": "updated-managed",
                      "reason": "native-user-mcp-saved-workspace-environment"})
    files[user_path] = json.dumps(user, ensure_ascii=False, indent=2) + "\n"
    files[project_path] = json.dumps(local, ensure_ascii=False, indent=2) + "\n"
