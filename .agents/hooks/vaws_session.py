#!/usr/bin/env python3
"""Native-client session attachment hook. Local only; never contacts the fleet."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_agent_session import AgentSessions, CLIENTS, load_context


# Cross-client payload discriminators, pinned by each client's documented hook
# contract and by the hook contract tests in session-management/tests:
# - Grok payloads use the camelCase field `hookEventName` and never carry
#   `cursor_version`; such a payload reaching the Claude or Cursor adapter is a
#   Grok/IDE compatibility import and must no-op.
# - A genuine Cursor payload carries `cursor_version` (Cursor hook input schema,
#   cursor.com/docs/hooks) and distinguishes its event with the snake_case field
#   `hook_event_name`; either marker means the payload is Cursor's own and the
#   Cursor adapter should attach.
# Only the client's own adapter may create a root attachment.
GROK_EVENT_FIELD = "hookEventName"
CURSOR_VERSION_FIELD = "cursor_version"


def handle(client: str, payload: dict, store: AgentSessions | None = None) -> dict:
    if client == "claude" and (GROK_EVENT_FIELD in payload or CURSOR_VERSION_FIELD in payload):
        return {}
    # Grok also imports Cursor hooks by default.
    if client == "cursor" and GROK_EVENT_FIELD in payload and CURSOR_VERSION_FIELD not in payload:
        return {}
    store = store or AgentSessions()
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    normalized = re.sub(r"[^a-z]", "", event.lower())
    native = str(payload.get("session_id") or payload.get("sessionId") or payload.get("conversation_id") or "")
    cwd = payload.get("cwd") or payload.get("workspaceRoot") or (payload.get("workspace_roots") or [str(Path.cwd())])[0]
    if not native:
        raise ValueError("hook has no native session identity; no task association was guessed")

    if normalized == "sessionstart":
        if payload.get("source") == "compact":
            context = store.native_context(client, native)
        else:
            parent = os.environ.get("VAWS_PARENT_CONTEXT", "")
            association = os.environ.get("VAWS_ATTACH_CONTEXT", "")
            if parent or association:
                inherited = load_context(parent or association)
                store = AgentSessions(Path(inherited["state_dir"]))
            context = store.attach(client, native, str(cwd), parent_context=parent, association=association)
            try:
                context = store.bind_sources(context, {Path(cwd).name: str(cwd)})
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # A new, non-Git or unborn project still has a usable local task.
                print(f"VAWS: source reference not yet bound: {type(exc).__name__}", file=sys.stderr)
    elif normalized in {"subagentstart", "subagentstop"}:
        parent_native = str(payload.get("parent_conversation_id") or payload.get("parentSessionId") or native)
        parent = store.native_context(client, parent_native)
        child = str(payload.get("agent_id") or payload.get("subagent_id") or payload.get("subagentId") or "")
        if not child:
            raise ValueError("client omitted the child id; do not invent a native session from its display name")
        context = store.attach(client, native, str(cwd), parent_context=parent["context_file"], agent_id=child)
        if normalized == "subagentstop":
            store.detach(context)
            return {}
    else:
        context = store.native_context(client, native, str(payload.get("agent_id") or ""))
        if normalized == "sessionend":
            store.detach(context)
            return {}

    hint = ("VAWS task context:\n" + context["context_file"] + "\n"
            "Pass this as context_file to vaws_session/vaws_run/vaws_execution/vaws_finish. "
            "Local editing needs no remote resources. For a child or authorized cross-tool handoff, "
            "pass this context explicitly; a new user-initiated task must create its own VAWS session.")
    if normalized == "pretooluse":
        name = str(payload.get("tool_name") or payload.get("toolName") or "")
        arguments = payload.get("tool_input") or payload.get("toolInput") or {}
        # Grok exposes MCP calls through its native `use_tool` dispatcher. Its
        # PreToolUse payload therefore puts the qualified MCP name and the
        # actual arguments one level deeper than Claude/Codex. Rewrite the
        # dispatcher envelope so the context receipt reaches the MCP server;
        # never flatten or infer the nested call from cwd/history.
        nested_key = ""
        nested_arguments = arguments
        if client == "grok" and isinstance(arguments, dict):
            nested_name = str(arguments.get("tool_name") or arguments.get("toolName") or "")
            if nested_name:
                name = nested_name
                nested_key = "tool_input" if "tool_input" in arguments else "toolInput"
                nested_arguments = arguments.get(nested_key) or {}
        if any(tool in name for tool in ("vaws_session", "vaws_run", "vaws_execution", "vaws_finish")) and client in {"claude", "codex", "grok"}:
            if not isinstance(nested_arguments, dict) or nested_arguments.get("context_file"):
                return {}
            updated = {**nested_arguments, "context_file": context["context_file"]}
            if nested_key:
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**arguments, nested_key: updated}}}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**arguments, "context_file": context["context_file"]}}}
        return {}
    if client == "cursor" and normalized == "sessionstart":
        return {"env": {"VAWS_CONTEXT_FILE": context["context_file"]}, "additional_context": hint}
    if client == "claude" and normalized == "sessionstart" and os.environ.get("CLAUDE_ENV_FILE"):
        with Path(os.environ["CLAUDE_ENV_FILE"]).open("a") as stream:
            stream.write("\nexport VAWS_CONTEXT_FILE=" + shlex.quote(context["context_file"]) + "\n")
    if normalized in {"sessionstart", "subagentstart", "userpromptsubmit"}:
        canonical = {"sessionstart": "SessionStart", "subagentstart": "SubagentStart", "userpromptsubmit": "UserPromptSubmit"}[normalized]
        return {"hookSpecificOutput": {"hookEventName": canonical, "additionalContext": hint}}
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    parser.add_argument("--project", type=Path, help="Scope a global hook (notably Kimi) to this project")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if args.project:
            cwd = Path(payload.get("cwd") or payload.get("workspaceRoot") or
                       (payload.get("workspace_roots") or [str(Path.cwd())])[0]).resolve()
            project = args.project.expanduser().resolve()
            if cwd != project and project not in cwd.parents:
                # Kimi appends stdout to the user prompt; a literal {} would
                # pollute every prompt outside this project. Stay silent.
                print("")
                return 0
        output = handle(args.client, payload)
        # Kimi's UserPromptSubmit contract appends returned text, rather than
        # relying on Claude's additionalContext extension.
        if args.client == "kimi" and payload.get("hook_event_name") == "UserPromptSubmit":
            print(output["hookSpecificOutput"]["additionalContext"] if output else "")
        else:
            print(json.dumps(output))
    except Exception as exc:
        print(f"VAWS local association unavailable: {exc}. Local tools remain usable.", file=sys.stderr)
        print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
