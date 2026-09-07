#!/usr/bin/env python3
"""Install scoped native session hooks and the two stdio MCP entries.

This configures files only. It does not grant client trust, change approval
policies, authenticate clients, run hooks, or contact a remote machine. It
never writes a bearer token and must not be applied to the operator's live
client configuration from tests.

Two logical providers are written when needed, because two repositories serve
two different things and neither proxies the other:

* `vaws-task` -> `.agents/scripts/vaws.py task-server`, which locates the
  coordinator checkout and serves `vaws_session` / `vaws_run` /
  `vaws_execution` / `vaws_finish`. Local attach/finish need no manager.
* `remote-dev` -> `.agents/scripts/remote_dev.py server`, which locates the
  remote-dev checkout and serves `remote_*`.

`--task-only` skips the remote-dev entry.

Preservation: existing user-managed servers and unknown fields are kept.
JSON merge does **not** rewrite `command` / `args` / `type` of a same-name
provider that already has them (the coordinator helper does, and that is not
accepted as a migration). TOML already preserves named servers. Stale
`mcp__remote-dev__vaws_*` permission rules are reported, never silently
rewritten.
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
from vaws_coordinator import coordinator_environment
from vaws_local_state import agent_sessions_root
from vaws_remote_dev import ASCEND_RUNTIME_ENV_FILE, resolver_spec, state_dir

CLIENTS = {"claude", "grok", "kimi", "codex", "cursor"}
EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "PreToolUse", "UserPromptSubmit")
REMOTE_DEV_LAUNCHER = ROOT / ".agents/scripts/remote_dev.py"
TASK_LAUNCHER = ROOT / ".agents/scripts/vaws.py"
BACKUP_DIR = ROOT / ".vaws-local/client-setup-backups"
TASK_SERVER_NAME = "vaws-task"
REMOTE_DEV_SERVER_NAME = "remote-dev"
HOOK_TIMEOUT_SECONDS = 12
STALE_TOOL_PREFIX_MARKERS = (
    "mcp__remote-dev__vaws_",
    "mcp__remote_dev__vaws_",
    "mcp__remote-dev__vaws.",
    "mcp__remote_dev__vaws.",
)


def remote_dev_server_args():
    return [str(REMOTE_DEV_LAUNCHER), "server"]


def task_server_args():
    return [str(TASK_LAUNCHER), "task-server"]


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


def task_server_env():
    """Environment the task server needs so it shares this workspace's registry."""
    env = coordinator_environment()
    keys = (
        "VAWS_AGENT_SESSIONS_DIR",
        "VAWS_PARITY_SCRIPT",
        "VAWS_PARITY_WORKSPACE_ROOT",
        "VAWS_HOST_QUEUE_MODULE",
        "VAWS_MACHINE_INVENTORY",
    )
    payload = {key: env[key] for key in keys if key in env}
    if env.get("VAWS_REMOTE_DEV_ROOT"):
        payload["VAWS_REMOTE_DEV_ROOT"] = env["VAWS_REMOTE_DEV_ROOT"]
    return payload


def desired_mcp_servers(*, task_only=False):
    """Ordered `{server name: entry}` for every stdio entry this helper owns."""
    servers = {}
    if not task_only:
        servers[REMOTE_DEV_SERVER_NAME] = {
            "command": sys.executable,
            "args": remote_dev_server_args(),
            "type": "stdio",
            "timeout": 600000,
            "env": remote_dev_env(),
        }
    servers[TASK_SERVER_NAME] = {
        "command": sys.executable,
        "args": task_server_args(),
        "type": "stdio",
        "timeout": 600000,
        "env": task_server_env(),
    }
    return servers


def hook_groups(client, project):
    command = shlex.join([
        sys.executable,
        str(ROOT / ".agents/hooks/vaws_session.py"),
        "--client", client,
        "--project", str(project),
    ])
    if client == "cursor":
        return {
            event[0].lower() + event[1:]: [{"command": command}]
            for event in EVENTS if event not in {"PreToolUse", "UserPromptSubmit"}
        }
    return {
        event: [{"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}]}]
        for event in EVENTS
    }


def merge_server_entry(existing, desired):
    """Fill missing keys from `desired`; never overwrite user command/args/type.

    User env keys win over defaults. Unknown fields on the existing entry stay.
    """
    merged = {**desired, **existing}
    desired_env = dict(desired.get("env") or {})
    existing_env = dict(existing.get("env") or {})
    if desired_env or existing_env:
        merged["env"] = {**desired_env, **existing_env}
    preserved = any(
        key in existing and existing.get(key) != desired.get(key)
        for key in ("command", "args", "type")
    )
    return merged, preserved


def stale_prefix_hits(text):
    return [marker for marker in STALE_TOOL_PREFIX_MARKERS if marker in text]


def merge_json(path, *, hooks=None, mcp=None, notes=None):
    value = json.loads(path.read_text()) if path.exists() else {}
    notes = [] if notes is None else notes
    if hooks:
        target = value.setdefault("hooks", {})
        for event, groups in hooks.items():
            existing = target.setdefault(event, [])
            commands = {entry.get("command", "") for group in groups for entry in group.get("hooks", [group])}
            existing[:] = [
                group for group in existing
                if not any(entry.get("command", "") in commands for entry in group.get("hooks", [group]))
            ]
            existing.extend(groups)
        if path.parent.name == ".cursor":
            value.setdefault("version", 1)
    if mcp:
        servers = value.setdefault("mcpServers", {})
        for name, desired in mcp.items():
            existing = servers.get(name)
            if existing is None:
                servers[name] = dict(desired)
                continue
            merged, preserved = merge_server_entry(existing, desired)
            servers[name] = merged
            if preserved:
                notes.append({
                    "path": str(path),
                    "server": name,
                    "action": "preserved",
                    "fields": ["command", "args", "type"],
                    "reason": "existing-named-server",
                })
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    for marker in stale_prefix_hits(text):
        notes.append({
            "path": str(path),
            "action": "reported",
            "marker": marker,
            "reason": "stale-tool-prefix-permission",
        })
    return text


def toml_server_body(key, entry):
    body = f"[mcp_servers.{key}]\ncommand = " + json.dumps(entry["command"]) + "\n"
    body += "args = " + json.dumps(entry["args"]) + "\n"
    env = entry.get("env") or {}
    if env:
        body += f"\n[mcp_servers.{key}.env]\n"
        body += "".join(item + " = " + json.dumps(value) + "\n" for item, value in env.items())
    return body


def configuration(client, project, *, kimi_config=None, task_only=False):
    return build_plan(client, project, kimi_config=kimi_config, task_only=task_only)["files"]


def build_plan(client, project, *, kimi_config=None, task_only=False):
    project = project.expanduser().resolve(strict=True)
    groups = hook_groups(client, project)
    servers = desired_mcp_servers(task_only=task_only)
    files = {}
    notes = []
    if client in {"claude", "cursor", "codex", "grok"}:
        relative = {
            "claude": ".claude/settings.local.json",
            "cursor": ".cursor/hooks.json",
            "codex": ".codex/hooks.json",
            "grok": ".grok/hooks/vaws-session.json",
        }[client]
        path = project / relative
        files[path] = merge_json(path, hooks=groups, notes=notes)
    if client in {"claude", "cursor", "kimi"}:
        path = project / {
            "claude": ".mcp.json",
            "cursor": ".cursor/mcp.json",
            "kimi": ".kimi-code/mcp.json",
        }[client]
        files[path] = merge_json(path, mcp=servers, notes=notes)
    if client in {"codex", "grok"}:
        path = project / ("." + client) / "config.toml"
        original = tomllib.loads(path.read_text()) if path.exists() else {}
        existing = original.get("mcp_servers", {})
        text = path.read_text() if path.exists() else ""
        changed = False
        for name, entry in servers.items():
            key = name.replace("-", "_")
            if any(candidate in existing for candidate in (key, name)):
                notes.append({
                    "path": str(path),
                    "server": name,
                    "action": "preserved",
                    "reason": "existing-named-server",
                })
                continue
            text = managed_toml_text(text, name, toml_server_body(key, entry))
            changed = True
        if changed:
            files[path] = text
        for marker in stale_prefix_hits(text or (path.read_text() if path.exists() else "")):
            notes.append({
                "path": str(path),
                "action": "reported",
                "marker": marker,
                "reason": "stale-tool-prefix-permission",
            })
    if client == "kimi":
        path = kimi_config or Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))) / "config.toml"
        command = groups["SessionStart"][0]["hooks"][0]["command"]
        body = "\n".join(
            "[[hooks]]\nevent = " + json.dumps(event) + "\ncommand = " + json.dumps(command)
            + "\ntimeout = " + str(HOOK_TIMEOUT_SECONDS) + "\n"
            for event in EVENTS
        )
        project_key = hashlib.sha256(str(project).encode()).hexdigest()[:16]
        files[path] = managed_toml_text(path.read_text() if path.exists() else "", "session-" + project_key, body)
    return {
        "files": files,
        "mcp_servers": {name: entry["args"] for name, entry in servers.items()},
        "notes": notes,
        "task_registry": str(agent_sessions_root()),
    }


def managed_toml_text(original, name, text):
    begin, end = f"# BEGIN VAWS {name}\n", f"# END VAWS {name}\n"
    if begin in original:
        before, rest = original.split(begin, 1)
        _, after = rest.split(end, 1)
        original = before + after
    result = original.rstrip() + "\n\n" + begin + text.rstrip() + "\n" + end
    tomllib.loads(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--kimi-config", type=Path, help="Kimi's actual user config if launched with --config-file")
    parser.add_argument("--task-only", action="store_true", help="Write only the vaws-task entry; skip remote-dev")
    parser.add_argument("--apply", action="store_true", help="Write with private backups; default is preview")
    args = parser.parse_args()
    plan = build_plan(args.client, args.project, kimi_config=args.kimi_config, task_only=args.task_only)
    changed = []
    for path, content in plan["files"].items():
        if path.exists() and path.read_text() == content:
            continue
        item = {"path": str(path), "sha256": hashlib.sha256(content.encode()).hexdigest()}
        if args.apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
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
    print(json.dumps({
        "state": "configured" if args.apply else "preview",
        "files": changed,
        "mcp_servers": plan["mcp_servers"],
        "notes": plan["notes"],
        "stale_tool_prefix_permissions": [
            note for note in plan["notes"] if note.get("reason") == "stale-tool-prefix-permission"
        ],
        "preserved_servers": [
            note for note in plan["notes"] if note.get("reason") == "existing-named-server"
        ],
        "trust_granted": False,
        "connected": False,
        "next": (
            "Review native client trust/approval prompts for each listed server, "
            "restart or resume the client, then verify actual calls. "
            "Do not treat this helper as a rewrite of hand-managed providers."
        ),
    }))


if __name__ == "__main__":
    main()
