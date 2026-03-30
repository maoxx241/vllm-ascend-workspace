import argparse
import sys
from pathlib import Path
from typing import List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.lib.config import RepoPaths
from tools.lib.doctor import doctor, init
from tools.lib.gitflow import default_base_ref
from tools.lib.session import create_session, status_session, switch_session
from tools.lib.targets import ensure_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaws")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    subparsers.add_parser("init")
    subparsers.add_parser("sync")

    remotes_parser = subparsers.add_parser("remotes")
    remotes_subparsers = remotes_parser.add_subparsers(
        dest="remotes_command",
        required=True,
    )
    remotes_subparsers.add_parser("normalize")

    target_parser = subparsers.add_parser("target")
    target_subparsers = target_parser.add_subparsers(dest="target_command", required=True)
    ensure_parser = target_subparsers.add_parser("ensure")
    ensure_parser.add_argument("target_name")

    session_parser = subparsers.add_parser("session")
    session_subparsers = session_parser.add_subparsers(
        dest="session_command",
        required=True,
    )
    create_parser = session_subparsers.add_parser("create")
    create_parser.add_argument("session_name")
    switch_parser = session_subparsers.add_parser("switch")
    switch_parser.add_argument("session_name")
    session_subparsers.add_parser("status")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = RepoPaths(root=Path.cwd())

    if args.command == "doctor":
        return doctor(paths)
    if args.command == "init":
        return init(paths)
    if args.command == "sync":
        print("sync: compatibility command available; no sync actions yet")
        return 0
    if args.command == "remotes" and args.remotes_command == "normalize":
        try:
            print(default_base_ref(paths))
        except RuntimeError as exc:
            print(str(exc))
            return 1
        return 0
    if args.command == "target" and args.target_command == "ensure":
        return ensure_target(paths, args.target_name)
    if args.command == "session" and args.session_command == "create":
        return create_session(paths, args.session_name)
    if args.command == "session" and args.session_command == "switch":
        return switch_session(paths, args.session_name)
    if args.command == "session" and args.session_command == "status":
        return status_session(paths)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
