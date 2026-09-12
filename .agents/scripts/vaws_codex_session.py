#!/usr/bin/env python3
"""Route a Codex native event to its actual workspace's selected environment.

The user hook definition stays fixed across worktrees and dependency updates.
This entry only selects an existing environment; native hooks retain task
identity, and native worktree setup retains creation and update ownership.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
sys.path.insert(0, str(ROOT / ".agents/scripts"))

from vaws_environment import PIN_ENV, saved_ready
from vaws_local_owner import accessible_windows_path
from vaws_native_task_env import task_env
from vaws_workspace_update import common_dir, git
from vaws_worktree_setup import unpinned_environment


def scoped_workspace(payload: dict, source: Path) -> Path | None:
    """Use only native cwd, including a subdirectory of a linked worktree."""
    value = payload.get("cwd")
    if not isinstance(value, str) or not value:
        return None
    cwd = Path(accessible_windows_path(value)).expanduser()
    if not cwd.is_absolute():
        return None
    try:
        target = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
        return target if common_dir(target).resolve() == common_dir(source).resolve() else None
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None


def forward(target: Path, payload: dict) -> int:
    receipt = saved_ready(target)
    environment = unpinned_environment()
    environment.update(task_env("codex", target))
    environment[PIN_ENV] = receipt["receipt"]
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    kind = "summary" if event == "Stop" else "session"
    name = "knowledge_summary.py" if kind == "summary" else "vaws_session.py"
    hook = target / ".agents/hooks" / name
    if not hook.is_file():
        # An explicitly selected older workspace may predate the thin hook.
        # The installed bootstrap still launches its selected package version.
        hook = ROOT / ".agents/hooks" / name
    command = [receipt["python"], str(hook), "--client", "codex", "--project", str(target),
               "--environment-receipt", receipt["receipt"]]
    print(json.dumps({"vaws_codex_hook": kind, "workspace": str(target),
                      "python": command[0], "receipt": receipt["receipt"]}), file=sys.stderr, flush=True)
    return subprocess.run(command, input=json.dumps(payload), text=True,
                          cwd=target, env=environment, check=False).returncode


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Codex hook input must be an object")
        target = scoped_workspace(payload, ROOT)
        if target is None:
            return 0
        return forward(target, payload)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "phase": "codex_session_hook", "error": str(exc)}),
              file=sys.stderr, flush=True)
        return 0  # Association/knowledge failures do not block ordinary local work.


if __name__ == "__main__":
    raise SystemExit(main())
