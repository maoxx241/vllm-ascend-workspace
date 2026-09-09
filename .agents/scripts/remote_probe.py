#!/usr/bin/env python3
"""Thin CLI over ``remote-dev`` probe. Command flags are unchanged."""
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
    parser = argparse.ArgumentParser(description="Probe a VAWS remote target.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args(argv)
    translated = [*selector_args(args), "--timeout-ms", str(int(args.timeout * 1000))]
    return run_cli_tool("probe", translated)


if __name__ == "__main__":
    raise SystemExit(main())
