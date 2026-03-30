import argparse
import sys
from pathlib import Path
from typing import List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.lib.config import RepoPaths
from tools.lib.doctor import doctor, init
from tools.lib.targets import ensure_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaws")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    subparsers.add_parser("init")

    target_parser = subparsers.add_parser("target")
    target_subparsers = target_parser.add_subparsers(dest="target_command", required=True)
    ensure_parser = target_subparsers.add_parser("ensure")
    ensure_parser.add_argument("target_name")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = RepoPaths(root=Path.cwd())

    if args.command == "doctor":
        return doctor(paths)
    if args.command == "init":
        return init(paths)
    if args.command == "target" and args.target_command == "ensure":
        return ensure_target(paths, args.target_name)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
