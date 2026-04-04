#!/usr/bin/env python3
"""Manage the local workspace machine profile.

The profile is untracked local state used to derive container names and other
collision-sensitive identifiers for machine-management.
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
    PROFILE_PATH,
    WorkspaceStateError,
    default_container_name,
    ensure_profile,
    profile_summary,
    validate_machine_username,
)


def print_json(data: dict[str, object]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_summary(args: argparse.Namespace) -> int:
    payload = profile_summary(path=args.profile_path)
    print_json(payload)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    normalized = validate_machine_username(args.username)
    print_json(
        {
            "success": True,
            "input": args.username,
            "machine_username": normalized,
            "container_name": default_container_name(normalized),
        }
    )
    return 0


def cmd_ensure(args: argparse.Namespace) -> int:
    profile, action = ensure_profile(
        path=args.profile_path,
        machine_username=args.username,
        allow_update=args.allow_update,
        generate=args.generate,
    )
    payload = {
        "success": True,
        "action": action,
        **profile_summary(path=args.profile_path),
        "machine_username": profile["machine_username"],
        "container_name": profile["container_name"],
        "source": profile.get("source"),
    }
    print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile-path",
        type=Path,
        default=PROFILE_PATH,
        help=f"profile path (default: {PROFILE_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary = subparsers.add_parser("summary", help="print the current workspace machine profile summary")
    summary.set_defaults(func=cmd_summary)

    validate = subparsers.add_parser("validate", help="validate one proposed machine username")
    validate.add_argument("username", help="letters and digits only; case-insensitive")
    validate.set_defaults(func=cmd_validate)

    ensure = subparsers.add_parser(
        "ensure",
        help="ensure the local workspace machine profile exists; use --username or --generate when the profile is missing",
    )
    ensure.add_argument("--username", help="letters and digits only; case-insensitive")
    ensure.add_argument(
        "--generate",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="generate a default machine username when the profile is missing; only use after the user accepted the default option",
    )
    ensure.add_argument(
        "--allow-update",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="allow replacing an existing machine username",
    )
    ensure.set_defaults(func=cmd_ensure)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WorkspaceStateError as exc:
        print_json({"success": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
