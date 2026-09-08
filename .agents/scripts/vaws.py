#!/usr/bin/env python3
"""Launch the extracted coordinator and the four local-first task operations.

vaws-coordinator (`vllm-ascend-workspace/vaws-coordinator`) is no longer
vendored under `.agents/coordinator/` or `.agents/lib/vaws_agent_session.py`.
Every task-facing call goes through this launcher so one place knows where the
checkout is and which environment it needs:

    VAWS_AGENT_SESSIONS_DIR   the single local task registry
    VAWS_HOST_QUEUE_MODULE    override only; coordinator defaults to its bundled host queue
    VAWS_MACHINE_INVENTORY    shared inventory JSON
    VAWS_PARITY_SCRIPT        scaffold `remote_code_parity.py`
    VAWS_REMOTE_DEV_ROOT      optional; local attach/finish do not need it

There is no default manager `--state-dir`. Requesting remote execution without
a manager is blocked/unavailable; this process never fabricates readiness.

Subcommands:

    status              JSON: checkout location, revision, pin drift
    bootstrap           clone / fast-forward the pinned revision
    env [--json]        print the coordinator environment
    hook                exec the coordinator native-session hook
    task-server         exec the stdio MCP server for vaws_* tools
    attach|session|run|execution|finish
                        exec the coordinator CLI of the same name

Progress goes to stderr. `status`, `bootstrap` and `env --json` print one JSON
object on stdout. `hook`, `task-server` and the four task operations replace
this process when the checkout is present.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))
from vaws_coordinator import (  # noqa: E402
    COORDINATOR_ROOT_ENV,
    CoordinatorUnavailable,
    checkout_commit,
    checkout_status,
    coordinator_environment,
    coordinator_root,
    default_checkout_dir,
    load_dependency,
    looks_like_checkout,
)
from vaws_dependency import (  # noqa: E402
    USABLE_STATES,
    hook_skip_message,
    inspect,
    record_hook_degradation,
    status_exit_code,
)

LAUNCHER_OPS = {"status", "bootstrap", "env", "hook", "task-server"}
TASK_OPS = {"attach", "session", "run", "execution", "finish"}


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _fallback_make_result(*, tool, target, outcome, status, summary, preview=None, extra=None, **_ignored):
    return {
        "schema_version": "remote-dev.result.v1",
        "tool": tool,
        "target": target,
        "outcome": outcome,
        "status": status,
        "summary": summary,
        "preview": preview or {},
        "refs": {},
        **(extra or {}),
    }


def error_payload(tool: str, *, outcome: str, status: str, error: str) -> dict:
    result = _fallback_make_result(
        tool=tool,
        target={"kind": "vaws-task"},
        outcome=outcome,
        status=status,
        summary=f"{tool} {status}.",
        preview={"stderr": error[-4000:]},
        extra={"error": error},
    )
    return {"text": result["summary"] + "\n" + error + "\n", "result": result}


def unavailable(operation: str) -> int:
    try:
        coordinator_root()
    except CoordinatorUnavailable as exc:
        message = str(exc)
    else:
        message = f"{COORDINATOR_ROOT_ENV} is set but the checkout cannot serve {operation}"
    tool = "vaws." + operation
    payload = error_payload(
        tool,
        outcome="blocked",
        status="unavailable",
        error=(
            f"{operation} is served by vllm-ascend-workspace/vaws-coordinator; "
            f"{message} Local file and shell tools remain available. No remote success is implied."
        ),
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 1


def _exec(root: Path, relative: str, args: list[str]) -> int:
    script = root / relative
    if not script.is_file():
        progress(f"vaws-coordinator checkout {root} has no {relative}")
        return 2
    env = coordinator_environment()
    command = [sys.executable, str(script), *args]
    progress(f"exec {shlex.join([str(script), *args])}")
    os.execve(sys.executable, command, env)
    return 0  # pragma: no cover - execve does not return


def cmd_status(_args: argparse.Namespace) -> int:
    payload = checkout_status()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return status_exit_code({"vaws-coordinator": payload["state"]})


def _git(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def cmd_bootstrap(args: argparse.Namespace) -> int:
    pin = load_dependency()
    dest = Path(args.dest).expanduser() if args.dest else Path(
        os.environ.get(COORDINATOR_ROOT_ENV) or default_checkout_dir()
    ).expanduser()
    url = args.url or pin["url"]
    ref = args.ref or pin["ref"]
    commit = None if args.track_ref else (args.commit or pin["commit"])
    payload = {"dest": str(dest), "url": url, "ref": ref, "pinned_commit": commit}
    if dest.exists() and any(dest.iterdir()) and not (dest / ".git").exists():
        print(json.dumps({**payload, "state": "blocked", "error": f"{dest} exists and is not a git checkout"}))
        return 2
    if not (dest / ".git").exists():
        progress(f"cloning {pin['repository']} ({ref}) into {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = _git("clone", "--quiet", "--branch", ref, url, str(dest))
        if result.returncode != 0 and not args.url and shutil.which("gh"):
            progress("git clone failed; retrying through gh with its stored credentials")
            gh = subprocess.run(
                ["gh", "repo", "clone", pin["repository"], str(dest), "--", "--quiet", "--branch", ref],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            result = gh if gh.returncode == 0 else result
        if result.returncode != 0:
            print(json.dumps({
                **payload,
                "state": "failed",
                "error": result.stderr.strip()[-2000:],
                "hint": (
                    "pass --url with a URL your git can authenticate, or clone it by hand and set "
                    f"{COORDINATOR_ROOT_ENV}"
                ),
            }))
            return 1
    progress(f"fetching {ref} in {dest}")
    result = _git("fetch", "--quiet", "origin", ref, cwd=dest)
    if result.returncode != 0:
        print(json.dumps({**payload, "state": "failed", "error": result.stderr.strip()[-2000:]}))
        return 1
    if commit is None:
        result = _git("checkout", "--quiet", "-B", ref, "FETCH_HEAD", cwd=dest)
    else:
        result = _git("checkout", "--quiet", "--detach", commit, cwd=dest)
    if result.returncode != 0:
        print(json.dumps({
            **payload,
            "state": "failed",
            "error": result.stderr.strip()[-2000:],
            "hint": (
                f"pinned commit {commit or ref} is not reachable from {ref}; "
                "update .agents/deps/coordinator.json"
            ),
        }))
        return 1
    head = checkout_commit(dest)
    state = "ready" if looks_like_checkout(dest) else "invalid"
    print(json.dumps({
        **payload,
        "state": state,
        "commit": head,
        "pin_matches": (head == commit) if commit else None,
        "next": f"export {COORDINATOR_ROOT_ENV}={shlex.quote(str(dest))} if this is not the default location",
    }, ensure_ascii=False))
    return 0 if state == "ready" else 1


def cmd_env(args: argparse.Namespace) -> int:
    env = coordinator_environment()
    keys = [
        "VAWS_AGENT_SESSIONS_DIR",
        "VAWS_MACHINE_INVENTORY",
        "VAWS_PARITY_SCRIPT",
        "VAWS_PARITY_WORKSPACE_ROOT",
        "VAWS_REMOTE_DEV_ROOT",
        COORDINATOR_ROOT_ENV,
    ]
    payload = {key: env[key] for key in keys if key in env}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for key, value in payload.items():
            print(f"{key}={shlex.quote(value)}")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    info = inspect("vaws-coordinator")
    if info["state"] not in USABLE_STATES:
        sys.stdin.read()
        progress(hook_skip_message("vaws-coordinator", info))
        record_hook_degradation(hook="vaws", dep="vaws-coordinator", state=info["state"])
        print("")
        return 0
    return _exec(Path(info["path"]), "hooks/vaws_session.py", list(args.args))


def cmd_task_server(_args: argparse.Namespace) -> int:
    try:
        root = coordinator_root()
    except CoordinatorUnavailable as exc:
        progress(f"vaws-task MCP server cannot start: {exc}")
        return 2
    return _exec(root, "task_server.py", [])


def exec_task_cli(argv: list[str]) -> int:
    operation = argv[0] if argv else "session"
    try:
        root = coordinator_root()
    except CoordinatorUnavailable:
        return unavailable(operation)
    return _exec(root, "scripts/vaws.py", argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="report the configured checkout and pin drift")
    status.set_defaults(func=cmd_status)

    bootstrap = sub.add_parser("bootstrap", help="clone or update the pinned coordinator checkout")
    bootstrap.add_argument("--dest", help=f"checkout directory (default: ${COORDINATOR_ROOT_ENV} or the shared .vaws-local/vaws-coordinator)")
    bootstrap.add_argument("--url", help="override the repository URL from .agents/deps/coordinator.json")
    bootstrap.add_argument("--ref", help="override the branch from .agents/deps/coordinator.json")
    bootstrap.add_argument("--commit", help="override the pinned commit")
    bootstrap.add_argument("--track-ref", action="store_true", help="check out the branch tip instead of the pinned commit")
    bootstrap.set_defaults(func=cmd_bootstrap)

    env = sub.add_parser("env", help="print the coordinator environment")
    env.add_argument("--json", action="store_true")
    env.set_defaults(func=cmd_env)

    hook = sub.add_parser("hook", help="exec the coordinator native-session hook")
    hook.add_argument("args", nargs=argparse.REMAINDER)
    hook.set_defaults(func=cmd_hook)

    server = sub.add_parser("task-server", help="exec the stdio MCP server for vaws_* tools")
    server.set_defaults(func=cmd_task_server)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in TASK_OPS:
        if any(item in {"-h", "--help"} for item in argv):
            try:
                coordinator_root()
            except CoordinatorUnavailable:
                print(
                    f"usage: vaws.py {argv[0]} ...\n"
                    f"Served by vaws-coordinator. Set {COORDINATOR_ROOT_ENV} or run `vaws.py bootstrap`."
                )
                return 0
        return exec_task_cli(argv)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
