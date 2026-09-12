#!/usr/bin/env python3
"""Start a native client in an independent editing workspace.

Same command in PowerShell, bash or zsh:
  uv run --no-project python .agents/scripts/vaws_client.py codex
  uv run --no-project python .agents/scripts/vaws_client.py kimi --workspace PATH

New directories check upstream once before selecting code and dependencies.
Existing --workspace directories keep their code, environment and configuration.
Pass native arguments after --; resume with the original --workspace directory.
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
                       "CODEX_THREAD_ID", "CODEX_SESSION_ID", "VAWS_RELEASE_LAUNCH"}


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


def activated_client_environment(receipt: dict, environment=None) -> dict[str, str]:
    """Give native shells ordinary venv activation without shell-specific code."""
    result = dict(os.environ if environment is None else environment)
    scripts = str(Path(receipt["python"]).parent)
    result["PATH"] = scripts + os.pathsep + result.get("PATH", "")
    result["VIRTUAL_ENV"] = str(receipt["root"])
    result.pop("PYTHONHOME", None)
    return result


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
        from vaws_workspace_entry import copy_workspace_identity, prepare_session
        from vaws_environment import MANAGED_PIN_ENV, PIN_ENV, native_ready, saved_ready, select_environment
        from vaws_local_owner import managed_receipt, windows_mounted_workspace
        # Only a new editing directory selects new code. An explicit existing
        # directory (including native resume) owns its saved immutable receipts.
        existing = args.workspace is not None and args.workspace.expanduser().exists()
        release_launch = os.environ.get("VAWS_RELEASE_LAUNCH") == "1"
        os.environ.pop(PIN_ENV, None)
        os.environ.pop(MANAGED_PIN_ENV, None)
        source = args.workspace.expanduser().resolve() if existing else ROOT
        if existing:
            native = saved_ready(source)
            os.environ[PIN_ENV] = native["receipt"]
            if windows_mounted_workspace(source):
                os.environ[MANAGED_PIN_ENV] = saved_ready(source, target_platform="win32")["receipt"]
        elif not release_launch:
            print("VAWS: checking upstream before creating this session's editing directory...",
                  file=sys.stderr, flush=True)
            result = prepare_session(ROOT)
            print(json.dumps({"workspace_updates": result}, ensure_ascii=False), file=sys.stderr, flush=True)
            if result.get("state") not in {"disabled", "identity_pending", "needs_github_user", "identity_invalid"}:
                from vaws_workspace_update import prepared_source
                source = prepared_source(ROOT) or ROOT
        if source != ROOT and not existing:
            # Run the prepared revision's client wiring and dependencies.
            # The existing checkout remains unchanged for any active GUI task.
            try:
                launcher = source / ".agents/scripts/vaws_client.py"
                if not launcher.is_file():
                    raise WorkspaceCopyError("prepared revision has no native client launcher")
                prepared = native_ready(source)
                if windows_mounted_workspace(source):
                    # The Windows owner prepares Windows packages. A WSL CLI
                    # also needs its prepared native Linux receipt.
                    from vaws_environment import windows_ready
                    windows_ready(source)
                copy_workspace_identity(ROOT, source)
                environment = dict(os.environ)
                environment["VAWS_RELEASE_LAUNCH"] = "1"
                for name in ("VAWS_VENV_REEXEC", "VAWS_SKIP_VENV_REEXEC", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
                    environment.pop(name, None)
                os.execvpe(prepared["python"], [prepared["python"], str(launcher), *values], environment)
            except (OSError, RuntimeError, ValueError) as exc:
                print(json.dumps({"workspace_updates": {"state": "update_launch_pending", "error": str(exc)}},
                                 ensure_ascii=False), file=sys.stderr, flush=True)
                source = ROOT
        # An interpreter hop may execute main again. Carry the one-time startup
        # decision through that hop; run_client removes this internal marker.
        os.environ["VAWS_RELEASE_LAUNCH"] = "1"
        ensure_workspace_interpreter(repo_root=source)
        receipt = prepare_workspace(args.client, args.workspace, source=source)
        target = Path(receipt["workspace"])
        if not existing:
            native = native_ready(source)
        os.environ[PIN_ENV] = native["receipt"]
        if not existing:
            try:
                copy_workspace_identity(source, target)
            except (OSError, RuntimeError, ValueError) as exc:
                print(json.dumps({"workspace_updates": {"state": "identity_copy_pending", "error": str(exc)}},
                                 ensure_ascii=False), file=sys.stderr, flush=True)
            select_environment(target, native)
        managed = native if native["platform"] == "win32" else (
            managed_receipt(source) if windows_mounted_workspace(source) else None)
        if managed is not None:
            os.environ[MANAGED_PIN_ENV] = managed["receipt"]
            if not existing and managed["key"] != native["key"]:
                select_environment(target, managed)
        import vaws_client_setup
        if existing:
            # Existing hooks/MCP commands already contain the session's pins.
            provider = vaws_client_setup.existing_task_env(args.client, target)
        else:
            plan = vaws_client_setup.build_plan(args.client, target)
            receipt["configuration"] = vaws_client_setup.apply_plan(plan)
            provider = vaws_client_setup.launch_env(args.client, target)
        print(json.dumps(receipt, ensure_ascii=False), file=sys.stderr, flush=True)
        environment = activated_client_environment(native)
        from vaws_local_owner import accessible_windows_path
        for key in ("VAWS_AGENT_SESSIONS_DIR", "VAWS_COORDINATOR_STATE_DIR", "VAWS_GITHUB_IDENTITY_FILE"):
            if key in provider:
                environment[key] = accessible_windows_path(provider[key])
        return run_client([*command, *native_args], target, environment=environment)
    except (WorkspaceCopyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
