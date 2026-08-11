#!/usr/bin/env python3
"""Manage the repo-local workspace/agent identity.

The UUID is generated silently and persisted under ``.vaws-local``.  The
optional alias is a cooperative display and resource namespace shared by the
project, newly launched containers, service runtime directories, and agents.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import (  # noqa: E402
    IDENTITY_PATH,
    WorkspaceStateError,
    ensure_workspace_identity,
    set_workspace_alias,
    validate_workspace_alias,
    workspace_identity_summary,
)


def print_json(data: dict[str, object]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_summary(args: argparse.Namespace) -> int:
    print_json(workspace_identity_summary(path=args.identity_path, ensure=False))
    return 0


def cmd_ensure(args: argparse.Namespace) -> int:
    identity, action = ensure_workspace_identity(path=args.identity_path)
    print_json(
        {
            "success": True,
            "action": action,
            "identity": identity,
            "summary": workspace_identity_summary(path=args.identity_path),
        }
    )
    return 0


def cmd_validate_alias(args: argparse.Namespace) -> int:
    print_json({"success": True, "alias": validate_workspace_alias(args.alias)})
    return 0


def cmd_set_alias(args: argparse.Namespace) -> int:
    identity, action = set_workspace_alias(args.alias, path=args.identity_path)
    print_json({"success": True, "action": action, "identity": identity})
    return 0


def cmd_decline_alias(args: argparse.Namespace) -> int:
    identity, action = set_workspace_alias(
        None, path=args.identity_path, declined=True
    )
    print_json({"success": True, "action": action, "identity": identity})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--identity-path",
        type=Path,
        default=IDENTITY_PATH,
        help=f"identity path (default: {IDENTITY_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary = subparsers.add_parser("summary", help="show identity without creating it")
    summary.set_defaults(func=cmd_summary)

    ensure = subparsers.add_parser("ensure", help="silently create UUID4 identity when missing")
    ensure.set_defaults(func=cmd_ensure)

    validate = subparsers.add_parser("validate-alias", help="validate a proposed unified alias")
    validate.add_argument("alias")
    validate.set_defaults(func=cmd_validate_alias)

    set_alias = subparsers.add_parser("set-alias", help="set or replace the unified alias")
    set_alias.add_argument("alias")
    set_alias.set_defaults(func=cmd_set_alias)

    decline = subparsers.add_parser("decline-alias", help="record that no unified alias is wanted")
    decline.set_defaults(func=cmd_decline_alias)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except WorkspaceStateError as exc:
        print_json({"success": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
