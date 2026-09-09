#!/usr/bin/env python3
"""Thin CLI over ``remote-dev`` artifact-pull."""
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
    parser = argparse.ArgumentParser(description="Pull remote artifacts through SSH streaming.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--remote-path", required=True)
    parser.add_argument("--local-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_cli_tool(
        "artifact_pull",
        [*selector_args(args), "--remote-path", args.remote_path, "--local-dir", str(args.local_dir)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
