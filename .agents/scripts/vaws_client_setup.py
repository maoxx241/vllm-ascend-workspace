#!/usr/bin/env python3
"""Install scoped native session hooks and the stdio MCP entries.

This configures files only. It does not grant client trust, change approval
policies, authenticate clients, run hooks, or contact a remote machine. It
never writes a bearer token and must not be applied to the operator's live
client configuration from tests.

Three logical providers are written when needed:

* `vaws-task` -> `python -m vaws_coordinator task-server`, which serves
  `vaws_session` / `vaws_run` / `vaws_execution` / `vaws_finish`. Local
  attach/finish need no manager.
* `remote-dev` -> `python -m remote_dev.mcp.server`, which serves `remote_*`.
* `vaws-knowledge` -> `python -m vaws_knowledge.server.mcp_server`, which
  serves `knowledge_query` / `knowledge_explain` / `knowledge_capture`.

`--task-only` writes only the vaws-task entry; it skips remote-dev and
vaws-knowledge.

Preservation: existing user-managed servers and unknown fields are kept.
JSON merge does **not** rewrite `command` / `args` / `type` of a living
same-name provider. A same-name entry whose command/args point at a path
inside this checkout that no longer exists is rewritten (`rewritten-stale`).
TOML follows the same rule. Stale `mcp__remote-dev__vaws_*` permission
rules are reported, never silently rewritten.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_venv import ensure_workspace_interpreter

ensure_workspace_interpreter(repo_root=ROOT)

import tomllib  # noqa: E402

from vaws_coordinator_launch import coordinator_environment
from vaws_knowledge_service import knowledge_server_env
from vaws_local_state import agent_sessions_root
from vaws_remote_dev import state_dir

CLIENTS = {"claude", "grok", "kimi", "codex", "cursor"}
EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "PreToolUse", "UserPromptSubmit")
BACKUP_DIR = ROOT / ".vaws-local/client-setup-backups"
TASK_SERVER_NAME = "vaws-task"
REMOTE_DEV_SERVER_NAME = "remote-dev"
KNOWLEDGE_SERVER_NAME = "vaws-knowledge"
HOOK_TIMEOUT_SECONDS = 12
STALE_TOOL_PREFIX_MARKERS = (
    "mcp__remote-dev__vaws_",
    "mcp__remote_dev__vaws_",
    "mcp__remote-dev__vaws.",
    "mcp__remote_dev__vaws.",
)


def remote_dev_server_args():
    return ["-m", "remote_dev.mcp.server"]


def task_server_args():
    return ["-m", "vaws_coordinator", "task-server"]


def knowledge_server_args():
    return ["-m", "vaws_knowledge.server.mcp_server"]


def remote_dev_env():
    """Environment the remote-dev MCP server needs; the launcher fills the same defaults."""
    return {
        "REMOTE_DEV_DEFAULT_USER": "root",
        "REMOTE_DEV_STATE_DIR": str(state_dir()),
    }


def _resolved_path(value):
    return str(Path(value).expanduser().resolve())


def task_server_env():
    """Environment the task server needs so it shares this workspace's registry."""
    env = coordinator_environment()
    keys = (
        "VAWS_AGENT_SESSIONS_DIR",
        "VAWS_COORDINATOR_STATE_DIR",
    )
    return {key: env[key] for key in keys if key in env}


def existing_task_env(client, project, *, kimi_config=None):
    """User-managed vaws-task env already on disk, if any."""
    if client in {"claude", "cursor", "kimi"}:
        path = project / {
            "claude": ".mcp.json",
            "cursor": ".cursor/mcp.json",
            "kimi": ".kimi-code/mcp.json",
        }[client]
        if path.is_file():
            try:
                servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
            except json.JSONDecodeError:
                return {}
            return dict((servers.get(TASK_SERVER_NAME) or {}).get("env") or {})
    if client in {"codex", "grok"}:
        path = project / ("." + client) / "config.toml"
        if path.is_file():
            try:
                servers = (tomllib.loads(path.read_text(encoding="utf-8")) or {}).get("mcp_servers") or {}
            except tomllib.TOMLDecodeError:
                return {}
            entry = servers.get("vaws_task") or servers.get("vaws-task") or {}
            return dict(entry.get("env") or {})
    return {}


def launch_env(client, project, *, kimi_config=None):
    """Setup defaults with existing provider env taking precedence."""
    return {**task_server_env(), **existing_task_env(client, project, kimi_config=kimi_config)}


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
    if not task_only:
        servers[KNOWLEDGE_SERVER_NAME] = {
            "command": sys.executable,
            "args": knowledge_server_args(),
            "type": "stdio",
            "timeout": 600000,
            "env": knowledge_server_env(ROOT),
        }
    return servers


def hook_command(client, project, env=None):
    """Self-contained hook command; a GUI client must not inherit setup's shell."""
    env = task_server_env() if env is None else env
    argv = [
        sys.executable,
        str(ROOT / ".agents/hooks/vaws_session.py"),
        "--client", client,
        "--project", str(project),
        "--agent-sessions-dir", env["VAWS_AGENT_SESSIONS_DIR"],
    ]
    return local_hook_command(argv)


def local_hook_command(argv):
    if os.name != "nt":
        return shlex.join(argv)
    # One portable launcher for clients which execute command strings through
    # cmd, PowerShell or a shell configured by the user. Paths stay literal,
    # including spaces, apostrophes, dollar signs and non-ASCII characters.
    literals = " ".join("'" + str(arg).replace("'", "''") + "'" for arg in argv)
    script = ("[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
              "$OutputEncoding = [Console]::OutputEncoding; $env:PYTHONIOENCODING='utf-8'; & ") + literals + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + encoded


def hook_argv(command):
    prefix = "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand "
    if not command.startswith(prefix):
        return shlex.split(command)
    try:
        script = base64.b64decode(command[len(prefix):], validate=True).decode("utf-16-le")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid generated hook command") from exc
    begin = ("[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
             "$OutputEncoding = [Console]::OutputEncoding; $env:PYTHONIOENCODING='utf-8'; & ")
    end = "; exit $LASTEXITCODE"
    if not script.startswith(begin) or not script.endswith(end):
        raise ValueError("not an owned hook launcher")
    body = script[len(begin):-len(end)]
    tokens = re.findall(r"'(?:[^']|'')*'", body)
    if " ".join(tokens) != body:
        raise ValueError("invalid literal hook arguments")
    return [token[1:-1].replace("''", "'") for token in tokens]


def hook_groups(client, project, env=None):
    command = hook_command(client, project, env)
    if client == "cursor":
        return {
            event[0].lower() + event[1:]: [{"command": command}]
            for event in EVENTS if event not in {"PreToolUse", "UserPromptSubmit"}
        }
    return {
        event: [{"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}]}]
        for event in EVENTS
    }


OWNED_HOOK_SCRIPT = ROOT / ".agents/hooks/vaws_session.py"


def _is_python_interpreter(program):
    name = Path(program).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name == "python" or name.startswith("python3")


def executed_hook_script(argv):
    """Return the script file a command would run, or None.

    A basename `vaws_session.py` anywhere in argv is not proof of ownership.
    Wrappers that pass our hook path as data are not owned.
    """
    index = 0
    while index < len(argv):
        item = argv[index]
        if item.startswith("-"):
            break
        if "=" in item:
            key = item.split("=", 1)[0]
            if key.isidentifier() and Path(item).suffix != ".py":
                index += 1
                continue
        break
    if index >= len(argv):
        return None
    program = argv[index]
    if _is_python_interpreter(program):
        index += 1
        while index < len(argv):
            item = argv[index]
            if item == "--":
                return argv[index + 1] if index + 1 < len(argv) else None
            if item in {"-c", "-m"}:
                return None
            if item.startswith("-"):
                if item in {"-W", "-X", "--check-hash-based-pycs"}:
                    index += 2
                    continue
                index += 1
                continue
            return item
        return None
    return program


def owned_hook_script(path, expected=None):
    try:
        wanted = Path(expected) if expected else OWNED_HOOK_SCRIPT
        if wanted.resolve() not in {OWNED_HOOK_SCRIPT.resolve(), (ROOT / ".agents/hooks/knowledge_summary.py").resolve()}:
            return False
        return Path(path).expanduser().resolve() == wanted.resolve()
    except OSError:
        return False


def owned_hook_command(command, client, project, expected=None):
    """True when `command` execs this checkout's hook for this client and project.

    Old generated commands (client/project only) and new ones (explicit
    root/registry flags) both match. A same-basename script in another path
    or worktree does not.
    """
    try:
        argv = hook_argv(command)
    except ValueError:
        return False
    script = executed_hook_script(argv)
    if not script or not owned_hook_script(script, expected=expected):
        return False
    parsed_client = None
    parsed_project = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--client" and index + 1 < len(argv):
            parsed_client = argv[index + 1]
            index += 2
            continue
        if item.startswith("--client="):
            parsed_client = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--project" and index + 1 < len(argv):
            parsed_project = argv[index + 1]
            index += 2
            continue
        if item.startswith("--project="):
            parsed_project = item.split("=", 1)[1]
            index += 1
            continue
        index += 1
    if parsed_client != client or not parsed_project:
        return False
    try:
        return Path(parsed_project).expanduser().resolve() == Path(project).expanduser().resolve()
    except OSError:
        return parsed_project == str(project)


def _desired_hook_command(groups):
    if not groups:
        return ""
    group = groups[0]
    if "hooks" in group:
        entries = group.get("hooks") or []
        return entries[0].get("command", "") if entries else ""
    return group.get("command", "")


def merge_hook_event(existing, desired, client, project):
    """Replace one owned hook entry; keep siblings and group metadata."""
    desired_command = _desired_hook_command(desired)
    expected = executed_hook_script(hook_argv(desired_command)) if desired_command else None
    replaced = False
    result = []
    for group in existing:
        if not isinstance(group, dict):
            result.append(group)
            continue
        if "hooks" in group:
            entries = []
            for entry in group.get("hooks") or []:
                if owned_hook_command(entry.get("command", ""), client, project, expected=expected):
                    if replaced:
                        continue
                    updated = dict(entry)
                    updated["command"] = desired_command
                    updated.setdefault("type", "command")
                    updated["timeout"] = desired[0]["hooks"][0].get("timeout", HOOK_TIMEOUT_SECONDS)
                    entries.append(updated)
                    replaced = True
                else:
                    entries.append(entry)
            if entries:
                updated_group = dict(group)
                updated_group["hooks"] = entries
                result.append(updated_group)
            continue
        command = group.get("command", "")
        if owned_hook_command(command, client, project, expected=expected):
            if replaced:
                continue
            updated = dict(group)
            updated["command"] = desired_command
            result.append(updated)
            replaced = True
        else:
            result.append(group)
    if not replaced:
        result.extend(desired)
    return result


def _looks_like_path(value):
    if not isinstance(value, str) or not value or value.startswith("-"):
        return False
    return value.startswith("/") or value.startswith(".") or "/" in value or value.endswith(".py")


def checkout_missing_refs(entry, checkout):
    """Paths inside ``checkout`` that the entry names but that no longer exist."""

    root = Path(checkout).expanduser().resolve()
    missing = []
    values = [entry.get("command"), *(entry.get("args") or [])]
    for value in values:
        if not _looks_like_path(value):
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not path.exists() and not resolved.exists():
            missing.append(str(value))
    return missing


def is_stale_scaffold_entry(existing, checkout):
    return bool(checkout_missing_refs(existing, checkout))


def merge_server_entry(existing, desired, *, checkout=None):
    """Fill missing keys from `desired`; keep a living user command/args/type.

    A same-name entry whose command/args point at a path inside this checkout
    that no longer exists is a leftover scaffold row, not a hand-written
    server, and is rewritten.
    """
    checkout = ROOT if checkout is None else checkout
    if is_stale_scaffold_entry(existing, checkout):
        merged = dict(desired)
        for key, value in existing.items():
            if key not in {"command", "args", "type", "env"}:
                merged.setdefault(key, value)
        desired_env = dict(desired.get("env") or {})
        existing_env = dict(existing.get("env") or {})
        if desired_env or existing_env:
            merged["env"] = {**desired_env, **existing_env}
        return merged, "rewritten-stale"
    merged = {**desired, **existing}
    desired_env = dict(desired.get("env") or {})
    existing_env = dict(existing.get("env") or {})
    if desired_env or existing_env:
        merged["env"] = {**desired_env, **existing_env}
    preserved = any(
        key in existing and existing.get(key) != desired.get(key)
        for key in ("command", "args", "type")
    )
    return merged, "preserved" if preserved else None


def stale_prefix_hits(text):
    return [marker for marker in STALE_TOOL_PREFIX_MARKERS if marker in text]


def mcp_server_aliases(name):
    """Hyphen name plus the underscore form TOML/JSON may already use."""
    aliases = []
    for alias in (name, name.replace("-", "_")):
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def merge_json(path, *, hooks=None, mcp=None, notes=None, client=None, project=None):
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    notes = [] if notes is None else notes
    if hooks:
        target = value.setdefault("hooks", {})
        for event, groups in hooks.items():
            target[event] = merge_hook_event(target.get(event) or [], groups, client, project)
        if path.parent.name == ".cursor":
            value.setdefault("version", 1)
    if mcp:
        servers = value.setdefault("mcpServers", {})
        checkout = project or ROOT
        for name, desired in mcp.items():
            aliases = mcp_server_aliases(name)
            found_keys = [alias for alias in aliases if alias in servers]
            if not found_keys:
                servers[name] = dict(desired)
                continue
            stale_keys = [
                alias for alias in found_keys
                if is_stale_scaffold_entry(servers[alias], checkout)
            ]
            if stale_keys:
                merged, action = merge_server_entry(
                    servers[stale_keys[0]], desired, checkout=checkout
                )
                for alias in aliases:
                    servers.pop(alias, None)
                servers[name] = merged
                notes.append({
                    "path": str(path),
                    "server": name,
                    "action": "rewritten-stale",
                    "fields": ["command", "args", "type"],
                    "reason": "stale-checkout-path",
                })
                continue
            source_key = found_keys[0]
            merged, action = merge_server_entry(
                servers[source_key], desired, checkout=checkout
            )
            servers[source_key] = merged
            if action == "preserved":
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


def fill_toml_server_env(text, key, existing, desired):
    """Add missing defaults to an ordinary env table; preserve user values/text."""
    missing = {name: value for name, value in (desired.get("env") or {}).items()
               if name not in (existing.get("env") or {})}
    if not missing:
        return text
    additions = "".join(json.dumps(name) + " = " + json.dumps(value) + "\n"
                        for name, value in missing.items())
    headers = {f"[mcp_servers.{key}.env]", f"[mcp_servers.{json.dumps(key)}.env]"}
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() in headers:
            lines.insert(index + 1, additions)
            result = "".join(lines)
            tomllib.loads(result)
            return result
    if "env" not in existing:
        result = text.rstrip() + f"\n\n[mcp_servers.{json.dumps(key)}.env]\n" + additions
        tomllib.loads(result)
        return result
    return text  # a hand-written inline env remains user-owned


def configuration(client, project, *, kimi_config=None, task_only=False):
    return build_plan(client, project, kimi_config=kimi_config, task_only=task_only)["files"]


def build_plan(client, project, *, kimi_config=None, task_only=False):
    project = project.expanduser().resolve(strict=True)
    env = launch_env(client, project, kimi_config=kimi_config)
    groups = hook_groups(client, project, env)
    if not task_only and client in {"codex", "claude", "cursor"}:
        summary_command = local_hook_command([
            sys.executable, str(ROOT / ".agents/hooks/knowledge_summary.py"),
            "--client", client, "--project", str(project),
        ])
        if client == "cursor":
            groups["afterAgentResponse"] = [{"command": summary_command}]
        else:
            groups["Stop"] = [{"hooks": [{"type": "command", "command": summary_command, "timeout": 5}]}]
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
        files[path] = merge_json(path, hooks=groups, notes=notes, client=client, project=project)
    if client in {"claude", "cursor", "kimi"}:
        path = project / {
            "claude": ".mcp.json",
            "cursor": ".cursor/mcp.json",
            "kimi": ".kimi-code/mcp.json",
        }[client]
        files[path] = merge_json(path, mcp=servers, notes=notes, project=project)
    if client in {"codex", "grok"}:
        path = project / ("." + client) / "config.toml"
        original = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        existing = original.get("mcp_servers", {})
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        changed = False
        for name, entry in servers.items():
            key = name.replace("-", "_")
            aliases = mcp_server_aliases(name)
            matching = [alias for alias in aliases if alias in existing]
            if matching:
                if any(is_stale_scaffold_entry(existing[alias], project) for alias in matching):
                    text = managed_toml_text(
                        drop_toml_server_tables(text, *aliases),
                        name,
                        toml_server_body(key, entry),
                    )
                    notes.append({
                        "path": str(path),
                        "server": name,
                        "action": "rewritten-stale",
                        "reason": "stale-checkout-path",
                    })
                    changed = True
                    continue
                if name == KNOWLEDGE_SERVER_NAME:
                    updated_text = fill_toml_server_env(text, matching[0], existing[matching[0]], entry)
                    changed = changed or updated_text != text
                    text = updated_text
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
        for marker in stale_prefix_hits(text or (path.read_text(encoding="utf-8") if path.exists() else "")):
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
        files[path] = managed_toml_text(path.read_text(encoding="utf-8") if path.exists() else "", "session-" + project_key, body)
    return {
        "files": files,
        "mcp_servers": {name: entry["args"] for name, entry in servers.items()},
        "notes": notes,
        "task_registry": str(agent_sessions_root()),
    }


def drop_toml_server_tables(original, *names):
    """Remove ``[mcp_servers.<name>]`` and dotted children for each name."""

    text = original
    for name in names:
        if not name:
            continue
        prefix = f"[mcp_servers.{name}"
        kept = []
        skipping = False
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                skipping = stripped.startswith(prefix) and (
                    stripped == prefix + "]" or stripped.startswith(prefix + ".")
                )
            if not skipping:
                kept.append(line)
        text = "".join(kept)
    return text


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
    parser.add_argument(
        "--task-only",
        action="store_true",
        help="Write only the vaws-task entry; skip remote-dev and vaws-knowledge",
    )
    parser.add_argument("--apply", action="store_true", help="Write with private backups; default is preview")
    args = parser.parse_args()
    plan = build_plan(args.client, args.project, kimi_config=args.kimi_config, task_only=args.task_only)
    changed = []
    for path, content in plan["files"].items():
        if path.exists() and path.read_text(encoding="utf-8") == content:
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
        "rewritten_servers": [
            note for note in plan["notes"] if note.get("action") == "rewritten-stale"
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
