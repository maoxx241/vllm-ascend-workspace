#!/usr/bin/env python3
"""Start a native client in an independent editing workspace.

Same command in PowerShell, bash or zsh:
  uv run --no-project python .agents/scripts/vaws_client.py codex
  uv run --no-project python .agents/scripts/vaws_client.py kimi --workspace PATH

An existing --workspace is reused; a missing path is created from the current
workspace including local changes. With no path, a new directory is allocated.
Pass native client arguments after -- (including explicit native resume IDs).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_native_workspace import WorkspaceCopyError, create_workspace, git
from vaws_venv import ensure_workspace_interpreter

CLIENT_COMMANDS = {"codex": "codex", "grok": "grok", "kimi": "kimi", "claude": "claude", "cursor": "cursor-agent"}
NATIVE_IDENTITY_ENV = {"VAWS_CONTEXT_FILE", "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT",
                       "CODEX_THREAD_ID", "CODEX_SESSION_ID"}


def resolve_client(client: str) -> list[str]:
    executable = shutil.which(CLIENT_COMMANDS[client])
    if not executable and client == "kimi":
        candidate = Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))) / "bin" / ("kimi.exe" if os.name == "nt" else "kimi")
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise WorkspaceCopyError(f"{client} is not installed in this environment; install its native CLI before starting it")
    return [executable]


def prepare_workspace(client: str, workspace: Path | None, *, source: Path = ROOT) -> dict:
    target = workspace.expanduser().absolute() if workspace else source / ".vaws-local/workspaces" / (client + "-" + uuid.uuid4().hex[:12])
    if target.exists():
        actual = Path(os.fsdecode(git(target, "rev-parse", "--show-toplevel").strip())).resolve()
        if actual != target.resolve():
            raise WorkspaceCopyError("--workspace must name the root of an existing Git checkout")
        return {"state": "reused", "workspace": str(target), "head": git(target, "rev-parse", "HEAD").decode().strip()}
    return create_workspace(source, target)


def client_environment(environment=None) -> dict[str, str]:
    # A new native session obtains identity from its own hook. Starting from
    # another agent process does not implicitly join that agent's task.
    return {key: value for key, value in (os.environ if environment is None else environment).items()
            if key not in NATIVE_IDENTITY_ENV}


def run_client(command: list[str], workspace: Path, *, environment=None) -> int:
    environment = client_environment(environment)
    if os.name == "nt":
        from vaws_windows import owned_process
        with owned_process(command, cwd=str(workspace), env=environment, stdin=sys.stdin,
                           stdout=sys.stdout, stderr=sys.stderr, release_on_exit=True,
                           preserve_console=True) as process:
            return process.wait()
    # Native interactive clients retain their terminal and signal behavior.
    # The process has its real cwd before any model or tool request can run.
    os.chdir(workspace)
    os.execvpe(command[0], command, environment)
    raise AssertionError("exec returned")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("client", choices=sorted(CLIENT_COMMANDS))
    parser.add_argument("--workspace", type=Path, help="reuse an existing checkout or create it at this path")
    values = list(sys.argv[1:] if argv is None else argv)
    split = values.index("--") if "--" in values else len(values)
    args = parser.parse_args(values[:split])
    native_args = values[split + 1:]
    try:
        command = resolve_client(args.client)
        from vaws_environment import MANAGED_PIN_ENV, PIN_ENV, native_ready, select_environment
        # Starting a new native client selects the current lock. Already running
        # hooks/MCP processes continue honoring their own immutable pin.
        os.environ.pop(PIN_ENV, None)
        os.environ.pop(MANAGED_PIN_ENV, None)
        ensure_workspace_interpreter(repo_root=ROOT)
        receipt = prepare_workspace(args.client, args.workspace)
        target = Path(receipt["workspace"])
        from vaws_local_owner import managed_receipt, windows_mounted_workspace
        native = native_ready(ROOT)
        select_environment(target, native)
        managed = native if native["platform"] == "win32" else (
            managed_receipt(ROOT) if windows_mounted_workspace(ROOT) else None)
        if managed is not None:
            os.environ[MANAGED_PIN_ENV] = managed["receipt"]
            if managed["key"] != native["key"]:
                select_environment(target, managed)
        import vaws_client_setup
        plan = vaws_client_setup.build_plan(args.client, target)
        receipt["configuration"] = vaws_client_setup.apply_plan(plan)
        print(json.dumps(receipt, ensure_ascii=False), file=sys.stderr, flush=True)
        environment = dict(os.environ)
        provider = vaws_client_setup.launch_env(args.client, target)
        from vaws_local_owner import accessible_windows_path
        for key in ("VAWS_AGENT_SESSIONS_DIR", "VAWS_COORDINATOR_STATE_DIR"):
            if key in provider:
                environment[key] = accessible_windows_path(provider[key])
        return run_client([*command, *native_args], target, environment=environment)
    except (WorkspaceCopyError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
