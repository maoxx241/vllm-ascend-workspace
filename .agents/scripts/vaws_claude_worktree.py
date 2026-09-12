#!/usr/bin/env python3
"""Create and prepare the Git worktree requested by Claude's WorktreeCreate hook.

Input is the native hook JSON on stdin. Stdout contains only the resulting
directory. Preparation facts and failures go to stderr and the new directory's
local receipt. The callback does not run on SessionStart or resume.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_workspace_update import git, write_json, Deferred


def prepare(source: Path, target: Path) -> dict:
    spec = importlib.util.spec_from_file_location("vaws_claude_native_setup", ROOT / ".agents/scripts/vaws_worktree_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.prepare_worktree("claude", source, target)


def create_worktree(payload: dict) -> tuple[Path, dict]:
    if payload.get("hook_event_name") != "WorktreeCreate":
        raise ValueError("this callback only handles Claude WorktreeCreate")
    name = payload.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", name) or ".." in name:
        raise ValueError("Claude worktree name must be a single Git-compatible slug")
    source = Path(payload["cwd"]).resolve()
    source = Path(git(source, "rev-parse", "--show-toplevel")).resolve()
    target = source / ".claude/worktrees" / name
    if target.exists():
        raise ValueError(f"requested worktree already exists; resume it instead: {target}")
    branch = "worktree/" + name
    git(source, "check-ref-format", "--branch", branch)
    git(source, "worktree", "add", "-b", branch, str(target), "HEAD")
    try:
        # The hook replaces Claude's ordinary file-copy step. Preserve the
        # project's native settings and other MCP providers before merging
        # this worktree's VAWS wiring. Never copy a task/session context.
        for relative in (".claude/settings.local.json", ".mcp.json"):
            old, new = source / relative, target / relative
            if old.is_file() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, new)
        result = prepare(source, target)
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"status": "failed", "phase": "claude_worktree_setup", "workspace": str(target),
                  "error": str(exc), **({"evidence": exc.evidence} if isinstance(exc, Deferred) else {})}
        write_json(target / ".vaws-local/claude-worktree-setup.json", result)
        # Leave the owned directory and raw facts available for repair; never
        # delete user files as compensation for failed dependency preparation.
        raise RuntimeError(f"new worktree retained at {target}: {exc}") from exc
    write_json(target / ".vaws-local/claude-worktree-setup.json", result)
    return target, result


def main() -> int:
    try:
        target, result = create_worktree(json.load(sys.stdin))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"status": "failed", "phase": "claude_worktree_setup", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    print(target, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
