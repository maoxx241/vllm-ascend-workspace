import argparse
import sys
from pathlib import Path
from typing import List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.lib.bootstrap import bootstrap_init, bootstrap_request_from_args
from tools.lib.config import RepoPaths
from tools.lib.doctor import doctor, init
from tools.lib.gitflow import default_base_ref
from tools.lib.session import create_session, status_session, switch_session
from tools.lib.targets import ensure_target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaws")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--bootstrap", action="store_true")
    init_parser.add_argument("--target-name", default="single-default")
    init_parser.add_argument("--host-name", default="host-a")
    init_parser.add_argument("--server-host")
    init_parser.add_argument("--server-user", default="root")
    init_parser.add_argument("--server-port", type=int, default=22)
    init_parser.add_argument("--server-auth-mode", default="ssh-key")
    init_parser.add_argument("--server-auth-group", default="default")
    init_parser.add_argument("--server-password-env")
    init_parser.add_argument("--server-key-path")
    init_parser.add_argument(
        "--runtime-image",
        default="quay.nju.edu.cn/ascend/vllm-ascend:latest",
    )
    init_parser.add_argument("--runtime-container", default="vaws-workspace")
    init_parser.add_argument("--runtime-ssh-port", type=int, default=63269)
    init_parser.add_argument(
        "--runtime-workspace-root",
        default="/vllm-workspace",
    )
    init_parser.add_argument("--vllm-origin-url")
    init_parser.add_argument("--vllm-ascend-origin-url")
    init_parser.add_argument("--git-auth-mode", default="ssh-key")
    init_parser.add_argument("--git-key-path")
    init_parser.add_argument("--git-token-env")
    sync_parser = subparsers.add_parser("sync")
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command")
    sync_start_parser = sync_subparsers.add_parser("start")
    sync_start_parser.add_argument("session_name")
    sync_subparsers.add_parser("status")
    sync_subparsers.add_parser("done")

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
        if args.bootstrap:
            try:
                request = bootstrap_request_from_args(args)
            except RuntimeError as exc:
                print(str(exc))
                return 1
            return bootstrap_init(paths, request)
        return init(paths)
    if args.command == "sync":
        if args.sync_command in (None, "status"):
            return status_session(paths)
        if args.sync_command == "start":
            if create_session(paths, args.session_name) != 0:
                return 1
            return switch_session(paths, args.session_name)
        if args.sync_command == "done":
            print("sync done: compatibility command reserved for archive/finish")
            return 0
        parser.error(f"unknown sync command: {args.sync_command}")
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
