#!/usr/bin/env python3
"""Exec one user-level MCP provider in the native client's selected workspace.

Kimi supplies the stdio cwd. Cursor supplies VAWS_MCP_WORKSPACE through its
native workspace variable. This selects only a saved package environment;
task identity remains the native attachment and is never inferred here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/scripts"))
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_claude_entry import PROVIDERS, launch_plan, workspace
from vaws_coordinator_launch import coordinator_environment
from vaws_environment import PIN_ENV
from vaws_knowledge_service import knowledge_server_env


def provider_plan(kind: str, cwd: Path, environment: dict) -> tuple[Path, list[str], dict]:
    native = Path(environment.get("VAWS_MCP_WORKSPACE") or cwd).expanduser().resolve(strict=True)
    try:
        target = workspace(native, source=ROOT)
    except ValueError:
        # A user-level provider may be visible in unrelated projects. Its
        # installed environment can list tools; task calls still need a real
        # native attachment, independently checked by the coordinator.
        target = ROOT
    command, env = launch_plan(kind, target, [], environment)
    env.pop("VAWS_MCP_WORKSPACE", None)
    if kind == "task":
        env = coordinator_environment(env, repo_root=target)
    elif kind == "remote":
        env.setdefault("REMOTE_DEV_DEFAULT_USER", "root")
        env.setdefault("REMOTE_DEV_STATE_DIR", str(target / ".vaws-local/remote-dev-state"))
    else:
        env = {**knowledge_server_env(target), **env}
    return native, command, env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=PROVIDERS)
    args = parser.parse_args(argv)
    try:
        cwd, command, environment = provider_plan(args.kind, Path.cwd(), dict(os.environ))
        print(json.dumps({"vaws_native_provider": args.kind, "cwd": str(cwd),
                          "python": command[0], "receipt": environment[PIN_ENV]}),
              file=sys.stderr, flush=True)
        os.chdir(cwd)
        if os.name == "nt":
            from vaws_windows import run_owned
            return run_owned(command, env=environment)
        os.execve(command[0], command, environment)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"VAWS {args.kind} provider unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
