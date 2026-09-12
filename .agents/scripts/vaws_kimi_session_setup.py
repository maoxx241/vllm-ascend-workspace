#!/usr/bin/env python3
"""Kimi native lifecycle adapter; requires the SessionSetup client extension.

Installed once as the project's scoped global hook. New sessions create their
editing directory before native workspace/MCP loading. Later events only route
to that directory's selected coordinator environment. Resume never updates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
sys.path.insert(0, str(ROOT / ".agents/scripts"))
from vaws_environment import PIN_ENV, MANAGED_PIN_ENV, saved_ready
from vaws_workspace_update import common_dir, git
from vaws_worktree_setup import prepare_worktree, unpinned_environment
from vaws_native_task_env import task_env
from vaws_native_workspace import create_linked_workspace


def scoped_source(project: Path, cwd: Path) -> Path | None:
    try:
        source = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
        return source if common_dir(source).resolve() == common_dir(project).resolve() else None
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None


def setup(project: Path, source: Path, payload: dict) -> dict:
    native = payload.get("session_id")
    if not isinstance(native, str) or not native:
        raise ValueError("Kimi SessionSetup omitted its native session_id")
    target = project / ".vaws-local/workspaces" / ("kimi-" + hashlib.sha256(native.encode()).hexdigest()[:20])
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        if payload.get("source") == "fork":
            create_linked_workspace(source, target)
        else:
            git(source, "worktree", "add", "--detach", str(target), "HEAD")
    elif common_dir(target).resolve() != common_dir(project).resolve():
        raise ValueError("Kimi setup target exists outside this Git worktree family")
    elif payload.get("source") == "fork" and not (target / ".vaws-local/native-workspace.json").is_file():
        raise ValueError(f"Kimi fork copy did not finish; incomplete worktree kept at {target}")
    if payload.get("source") == "fork":
        result = prepare_worktree("kimi", source, target, preserve_source=True)
    else:
        result = prepare_worktree("kimi", source, target)
    print(json.dumps({"kimi_session_setup": result}, ensure_ascii=False), file=sys.stderr, flush=True)
    return {"hookSpecificOutput": {"cwd": str(target)}}


def forward(source: Path, payload: dict) -> int:
    receipt = saved_ready(source)
    environment = unpinned_environment()
    environment.update(task_env("kimi", source))
    environment[PIN_ENV] = receipt["receipt"]
    command = [receipt["python"], str(source / ".agents/hooks/vaws_session.py"),
               "--client", "kimi", "--project", str(source),
               "--environment-receipt", receipt["receipt"]]
    result = subprocess.run(command, input=json.dumps(payload), text=True,
                            env=environment, cwd=source, check=False)
    return result.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    payload = {}
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Kimi hook input must be an object")
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return 0
        source = scoped_source(args.project.resolve(), Path(cwd).resolve())
        if source is None:
            return 0
        if payload.get("hook_event_name") == "SessionSetup":
            os.environ.pop(PIN_ENV, None)
            os.environ.pop(MANAGED_PIN_ENV, None)
            print(json.dumps(setup(args.project.resolve(), source, payload)), flush=True)
            return 0
        return forward(source, payload)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "phase": "kimi_session_setup", "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr, flush=True)
        # A configured new-session setup cannot silently start in the source
        # checkout after a failure. Ordinary association failures stay optional.
        return 2 if payload.get("hook_event_name") == "SessionSetup" else 0


if __name__ == "__main__":
    raise SystemExit(main())
