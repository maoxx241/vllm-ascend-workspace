#!/usr/bin/env python3
"""Thin CLI over ``remote-dev`` bash. Command flags are unchanged."""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=LIB_DIR.parent.parent)

from vaws_remote_dev import run_cli_tool  # noqa: E402
from vaws_remote_target import add_target_args, selector_args  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute a shell command on a VAWS remote target.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--cwd")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--timeout", type=float)
    parser.add_argument(
        "--no-runtime-env",
        action="store_true",
        help="do not source /etc/profile.d/vaws-ascend-env.sh before running the command",
    )
    parser.add_argument("--command", help="shell command to execute; alternatively pass after --")
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if not command and args.command_args:
        command_args = list(args.command_args)
        if command_args and command_args[0] == "--":
            command_args = command_args[1:]
        command = shlex.join(command_args)
    if not command:
        parser.error("--command or command after -- is required")
    translated = [*selector_args(args), "--command", command]
    if args.cwd:
        translated.extend(["--cwd", args.cwd])
    for item in args.env:
        translated.extend(["--env", item])
    if args.timeout is not None:
        translated.extend(["--timeout-ms", str(int(args.timeout * 1000))])
    if args.no_runtime_env:
        translated.append("--no-runtime-env")
    return run_cli_tool("bash", translated)


if __name__ == "__main__":
    raise SystemExit(main())
