#!/usr/bin/env python3
"""Thin CLI over ``remote-dev`` bash --run-in-background."""
from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description="Start a long-running remote job.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--cwd")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--kind", default="command")
    parser.add_argument("--job-id")
    parser.add_argument("--timeout", type=int)
    parser.add_argument(
        "--no-runtime-env",
        action="store_true",
        help="do not source /etc/profile.d/vaws-ascend-env.sh before running the job",
    )
    parser.add_argument("--command", required=True)
    args = parser.parse_args(argv)
    translated = [*selector_args(args), "--run-in-background", "--command", args.command]
    if args.cwd:
        translated.extend(["--cwd", args.cwd])
    for item in args.env:
        translated.extend(["--env", item])
    if args.timeout is not None:
        translated.extend(["--timeout-ms", str(int(args.timeout * 1000))])
    if args.no_runtime_env:
        translated.append("--no-runtime-env")
    if args.kind:
        translated.extend(["--description", args.kind])
    if args.job_id:
        sys.stderr.write(
            "remote_job_start: --job-id is accepted for surface compatibility but "
            "ignored; vaws-remote-dev assigns job ids. Use remote_job_status with "
            "the id returned by this command.\n"
        )
    return run_cli_tool("bash", translated)


if __name__ == "__main__":
    raise SystemExit(main())
