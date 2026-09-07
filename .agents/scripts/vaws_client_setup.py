#!/usr/bin/env python3
"""Install scoped native session hooks and the common remote-dev MCP entry.

This configures files only. It does not grant client trust, change approval
policies, authenticate clients, run hooks, or contact a remote machine.

The MCP entry runs `.agents/scripts/remote_dev.py server`, which locates the
external remote-dev checkout (`VAWS_REMOTE_DEV_ROOT` or the shared
`.vaws-local/remote-dev`) and injects the scaffold resolver, the Ascend runtime
environment file and the state directory. The entry's `env` block repeats the
values that matter to a reader so the generated configuration documents the
substrate contract on its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_agent_session import CLIENTS
from vaws_remote_dev import ASCEND_RUNTIME_ENV_FILE, resolver_spec, state_dir

EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "PreToolUse", "UserPromptSubmit")
REMOTE_DEV_LAUNCHER = ROOT / ".agents/scripts/remote_dev.py"
BACKUP_DIR = ROOT / ".vaws-local/client-setup-backups"


def remote_dev_server_args():
    return [str(REMOTE_DEV_LAUNCHER), "server"]


def remote_dev_env():
    """Environment the remote-dev MCP server needs; the launcher fills the same defaults."""
    return {
        "REMOTE_DEV_DEFAULT_USER": "root",
        "REMOTE_DEV_DEFAULT_ROOT": "/vllm-workspace",
        "REMOTE_DEV_DEFAULT_CWD": "/vllm-workspace",
        "REMOTE_DEV_RUNTIME_ENV_FILE": ASCEND_RUNTIME_ENV_FILE,
        "REMOTE_DEV_RESOLVERS": resolver_spec(),
        "REMOTE_DEV_STATE_DIR": str(state_dir()),
    }

# The hook performs up to two `git rev-parse` calls with a 5s timeout each
# (vaws_agent_session.worktree_reference); a 3s budget would kill a healthy hook.
HOOK_TIMEOUT_SECONDS = 12


def hook_groups(client, project):
    command = shlex.join([sys.executable, str(ROOT / ".agents/hooks/vaws_session.py"),
                          "--client", client, "--project", str(project)])
    if client == "cursor":
        return {event[0].lower() + event[1:]: [{"command": command}]
                for event in EVENTS if event not in {"PreToolUse", "UserPromptSubmit"}}
    return {event: [{"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}]}]
            for event in EVENTS}


def merge_json(path, *, hooks=None, mcp=False):
    value = json.loads(path.read_text()) if path.exists() else {}
    if hooks:
        target = value.setdefault("hooks", {})
        for event, groups in hooks.items():
            existing = target.setdefault(event, [])
            # Replace only this adapter for this project/client, preserving all
            # other hooks, including the user's policy and knowledge hooks.
            commands = {entry.get("command", "") for group in groups for entry in group.get("hooks", [group])}
            existing[:] = [group for group in existing if not any(
                entry.get("command", "") in commands for entry in group.get("hooks", [group]))]
            existing.extend(groups)
        if path.parent.name == ".cursor":
            value.setdefault("version", 1)
    if mcp:
        entry = value.setdefault("mcpServers", {}).setdefault("remote-dev", {})
        entry.update(command=sys.executable, args=remote_dev_server_args(), type="stdio")
        # Fill what the substrate needs; values the user already set stay.
        entry["env"] = {**remote_dev_env(), **entry.get("env", {})}
        entry.setdefault("timeout", 600000)
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def managed_toml(path, name, text):
    original = path.read_text() if path.exists() else ""
    begin, end = f"# BEGIN VAWS {name}\n", f"# END VAWS {name}\n"
    if begin in original:
        before, rest = original.split(begin, 1)
        _, after = rest.split(end, 1)
        original = before + after
    result = original.rstrip() + "\n\n" + begin + text.rstrip() + "\n" + end
    tomllib.loads(result)  # Refuse conflicting tables before writing anything.
    return result


def configuration(client, project, *, kimi_config=None):
    project = project.expanduser().resolve(strict=True)
    groups = hook_groups(client, project)
    files = {}
    if client in {"claude", "cursor", "codex", "grok"}:
        relative = {"claude": ".claude/settings.local.json", "cursor": ".cursor/hooks.json",
                    "codex": ".codex/hooks.json", "grok": ".grok/hooks/vaws-session.json"}[client]
        path = project / relative
        files[path] = merge_json(path, hooks=groups)
    if client in {"claude", "cursor", "kimi"}:
        path = project / {"claude": ".mcp.json", "cursor": ".cursor/mcp.json", "kimi": ".kimi-code/mcp.json"}[client]
        files[path] = merge_json(path, mcp=True)
    if client in {"codex", "grok"}:
        path = project / ("." + client) / "config.toml"
        original = tomllib.loads(path.read_text()) if path.exists() else {}
        servers = original.get("mcp_servers", {})
        # Never replace an existing server or edit credentials/policies. The
        # shared server gains the task tools after its normal client restart.
        if not any(name in servers for name in ("remote_dev", "remote-dev")):
            body = "[mcp_servers.remote_dev]\ncommand = " + json.dumps(sys.executable) + "\n"
            body += "args = " + json.dumps(remote_dev_server_args()) + "\n"
            body += "\n[mcp_servers.remote_dev.env]\n"
            body += "".join(key + " = " + json.dumps(value) + "\n" for key, value in remote_dev_env().items())
            files[path] = managed_toml(path, "remote-dev", body)
    if client == "kimi":
        path = kimi_config or Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))) / "config.toml"
        command = groups["SessionStart"][0]["hooks"][0]["command"]
        body = "\n".join("[[hooks]]\nevent = " + json.dumps(event) + "\ncommand = " + json.dumps(command) +
                         "\ntimeout = " + str(HOOK_TIMEOUT_SECONDS) + "\n" for event in EVENTS)
        project_key = hashlib.sha256(str(project).encode()).hexdigest()[:16]
        files[path] = managed_toml(path, "session-" + project_key, body)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--kimi-config", type=Path, help="Kimi's actual user config if launched with --config-file")
    parser.add_argument("--apply", action="store_true", help="Write with private backups; default is preview")
    args = parser.parse_args()
    files = configuration(args.client, args.project, kimi_config=args.kimi_config)
    changed = []
    for path, content in files.items():
        if path.exists() and path.read_text() == content:
            continue
        item = {"path": str(path), "sha256": hashlib.sha256(content.encode()).hexdigest()}
        if args.apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                # Pre-write backups live under the scaffold's own ignored
                # state root, not inside the (now external) substrate.
                directory = BACKUP_DIR
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                backup = directory / (hashlib.sha256(str(path).encode()).hexdigest()[:16] + "-" + str(time.time_ns()))
                backup.write_bytes(path.read_bytes())
                backup.chmod(0o600)
                item["backup"] = str(backup)
            temporary = path.with_name(path.name + ".vaws-" + str(time.time_ns()))
            temporary.write_text(content)
            temporary.chmod(0o600)
            os.replace(temporary, path)
        changed.append(item)
    print(json.dumps({"state": "configured" if args.apply else "preview", "files": changed,
                      "trust_granted": False, "connected": False,
                      "next": "Review native client trust/approval prompts, restart or resume the client, then verify actual calls."}))


if __name__ == "__main__":
    main()
