#!/usr/bin/env python3
"""Thin CLI over ``remote-dev`` job-tail."""
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
    parser = argparse.ArgumentParser(description="Tail a remote job log.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--stream", choices=("stdout", "stderr", "both"), default="both")
    args = parser.parse_args(argv)
    return run_cli_tool(
        "job_tail",
        [*selector_args(args), "--job-id", args.job_id, "--lines", str(args.lines), "--stream", args.stream],
    )


if __name__ == "__main__":
    raise SystemExit(main())
