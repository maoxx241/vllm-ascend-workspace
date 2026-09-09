#!/usr/bin/env python3
"""Launch the installed coordinator package and the four local-first task operations.

Every task-facing call goes through this launcher so one place injects the
scaffold environment the package needs:

    VAWS_AGENT_SESSIONS_DIR   the single local task registry
    VAWS_HOST_QUEUE_MODULE    override only; coordinator defaults to its bundled host queue
    VAWS_COORDINATOR_STATE_DIR
                              optional override for the coordinator-owned store

There is no default manager ``--state-dir``. Requesting remote execution without
a manager is blocked/unavailable; this process never fabricates readiness.

Subcommands:

    status              JSON: installed package vs uv.lock
    env [--json]        print the coordinator environment
    hook                exec the coordinator native-session hook
    task-server         exec ``python -m vaws_coordinator task-server``
    attach|session|run|execution|finish
                        exec ``python -m vaws_coordinator.vaws`` of the same name

Progress goes to stderr. ``status`` and ``env --json`` print one JSON object on
stdout. ``hook``, ``task-server`` and the four task operations replace this
process when the package is present.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))
from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_coordinator_launch import (  # noqa: E402
    CoordinatorUnavailable,
    coordinator_environment,
    exec_module,
    package_status,
    require_package,
)
from vaws_dependency import REMEDY, status_exit_code  # noqa: E402

LAUNCHER_OPS = {"status", "env", "hook", "task-server"}
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
        require_package()
        message = f"vaws-coordinator cannot serve {operation}"
    except CoordinatorUnavailable as exc:
        message = str(exc)
    tool = "vaws." + operation
    payload = error_payload(
        tool,
        outcome="blocked",
        status="unavailable",
        error=(
            f"{operation} is served by the vaws-coordinator package; "
            f"{message} Local file and shell tools remain available. No remote success is implied."
        ),
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 1


def cmd_status(_args: argparse.Namespace) -> int:
    payload = package_status()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return status_exit_code({"vaws-coordinator": payload["state"]})


def cmd_env(args: argparse.Namespace) -> int:
    env = coordinator_environment()
    keys = [
        "VAWS_AGENT_SESSIONS_DIR",
        "VAWS_COORDINATOR_STATE_DIR",
    ]
    payload = {key: env[key] for key in keys if key in env}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for key, value in payload.items():
            print(f"{key}={shlex.quote(value)}")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    try:
        require_package()
    except CoordinatorUnavailable as exc:
        sys.stdin.read()
        progress(f"vaws-coordinator hook skipped: {exc}")
        print("")
        return 0
    return exec_module("vaws_coordinator.hooks.vaws_session", list(args.args))


def cmd_task_server(_args: argparse.Namespace) -> int:
    try:
        require_package()
    except CoordinatorUnavailable as exc:
        progress(f"vaws-task MCP server cannot start: {exc}")
        return 2
    return exec_module("vaws_coordinator", ["task-server"])


def exec_task_cli(argv: list[str]) -> int:
    operation = argv[0] if argv else "session"
    try:
        require_package()
    except CoordinatorUnavailable:
        return unavailable(operation)
    return exec_module("vaws_coordinator.vaws", argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="report the installed package vs uv.lock")
    status.set_defaults(func=cmd_status)

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
                require_package()
            except CoordinatorUnavailable:
                print(
                    f"usage: vaws.py {argv[0]} ...\n"
                    f"Served by the vaws-coordinator package. Run `{REMEDY}`."
                )
                return 0
        return exec_task_cli(argv)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
