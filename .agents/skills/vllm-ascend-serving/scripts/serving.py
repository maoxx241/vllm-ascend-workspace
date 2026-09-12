#!/usr/bin/env python3
"""Start, inspect or stop one managed vLLM service."""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / '.agents/lib'))
from vaws_venv import ensure_workspace_interpreter

if __name__ == "__main__":
    from vaws_managed_entry import ensure_managed_entry
    ensure_managed_entry(repo_root=ROOT, entry_file=__file__)

ensure_workspace_interpreter(repo_root=ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('action', choices=('start', 'status', 'stop'))
    parser.add_argument('args', nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    module = importlib.import_module(f'_serving_{args.action}')
    return module.main(args.args)


if __name__ == '__main__':
    raise SystemExit(main())
