#!/usr/bin/env python3
"""Agent-facing machine-profile workflow for repo-init.

Prefer this wrapper during broad init instead of calling workspace_profile.py
and inferring a username ad hoc from surrounding context.

All outputs are JSON.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Sequence

LIB_DIR = pathlib.Path(__file__).resolve().parents[3] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import (  # noqa: E402
    WorkspaceStateError,
    ensure_profile,
    load_profile,
    profile_summary,
    validate_machine_username,
)

from _profile_choice_common import detect_git_username_candidate, fixed_machine_username_question  # noqa: E402


Status = str


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def status_payload(
    status: Status,
    *,
    success: bool,
    action: str,
    message: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": success,
        "status": status,
        "action": action,
    }
    if message is not None:
        payload["message"] = message
    payload.update(extra)
    return payload


def existing_profile_payload() -> dict[str, Any]:
    summary = profile_summary()
    return status_payload(
        "ready",
        success=True,
        action="existing",
        message="local machine profile already exists; broad init should reuse it unless the user explicitly asked to change it",
        profile=summary,
    )


def needs_choice_payload(cwd: pathlib.Path | None = None) -> dict[str, Any]:
    summary = profile_summary()
    return status_payload(
        "needs_input",
        success=False,
        action="choose-machine-username",
        message="local machine profile is missing; ask exactly one fixed-choice question before continuing broad init",
        profile=summary,
        question=fixed_machine_username_question(cwd),
    )


def cmd_plan(args: argparse.Namespace) -> int:
    summary = profile_summary()
    if summary["exists"]:
        print_json(existing_profile_payload())
        return 0
    print_json(needs_choice_payload(pathlib.Path.cwd()))
    return 0


def _create_profile_from_choice(choice: str, custom_username: str | None, cwd: pathlib.Path | None = None) -> dict[str, Any]:
    if choice == "git-username":
        detected = detect_git_username_candidate(cwd)
        if not detected["available"] or not detected["candidate"]:
            return status_payload(
                "blocked",
                success=False,
                action="git-username-unavailable",
                message="the Git username option is not currently available; choose random/custom or establish Git / GitHub identity first",
                detected_git_username=detected,
                profile=profile_summary(),
                question=fixed_machine_username_question(cwd),
            )
        profile, action = ensure_profile(machine_username=detected["candidate"], allow_update=False, generate=False)
        return status_payload(
            "ready",
            success=True,
            action=action,
            message="created the local machine profile from the detected Git username",
            selection={
                "choice": choice,
                "machine_username": detected["candidate"],
                "source": detected["source"],
            },
            profile={
                **profile_summary(),
                "machine_username": profile["machine_username"],
                "container_name": profile["container_name"],
                "source": profile.get("source"),
            },
        )

    if choice == "random":
        profile, action = ensure_profile(machine_username=None, allow_update=False, generate=True)
        return status_payload(
            "ready",
            success=True,
            action=action,
            message="created the local machine profile with a random agent##### username",
            selection={
                "choice": choice,
                "machine_username": profile["machine_username"],
                "pattern": "agent#####",
            },
            profile={
                **profile_summary(),
                "machine_username": profile["machine_username"],
                "container_name": profile["container_name"],
                "source": profile.get("source"),
            },
        )

    if custom_username is None:
        return status_payload(
            "needs_input",
            success=False,
            action="await-custom-machine-username",
            message="custom mode was selected; ask one follow-up question for the literal username before mutating anything",
            missing={
                "name": "custom_machine_username",
                "rules": profile_summary()["username_rules"],
                "question": "请输入你想使用的机器用户名（仅限英文和数字，3-32 位）",
            },
            question=fixed_machine_username_question(cwd),
        )

    try:
        normalized = validate_machine_username(custom_username)
    except WorkspaceStateError as exc:
        return status_payload(
            "needs_input",
            success=False,
            action="invalid-custom-machine-username",
            message=str(exc),
            invalid_input=custom_username,
            missing={
                "name": "custom_machine_username",
                "rules": profile_summary()["username_rules"],
                "question": "请输入你想使用的机器用户名（仅限英文和数字，3-32 位）",
            },
            question=fixed_machine_username_question(cwd),
        )

    profile, action = ensure_profile(machine_username=normalized, allow_update=False, generate=False)
    return status_payload(
        "ready",
        success=True,
        action=action,
        message="created the local machine profile from the explicit custom username",
        selection={
            "choice": choice,
            "machine_username": normalized,
        },
        profile={
            **profile_summary(),
            "machine_username": profile["machine_username"],
            "container_name": profile["container_name"],
            "source": profile.get("source"),
        },
    )


def cmd_apply(args: argparse.Namespace) -> int:
    current = load_profile()
    if current is not None:
        print_json(existing_profile_payload())
        return 0

    payload = _create_profile_from_choice(
        choice=args.choice,
        custom_username=args.custom_username,
        cwd=pathlib.Path.cwd(),
    )
    print_json(payload)
    return 0 if payload.get("success") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="summarize the exact machine-username choices for broad init")
    plan.set_defaults(func=cmd_plan)

    apply = subparsers.add_parser("apply", help="materialize one approved machine-username choice")
    apply.add_argument(
        "--choice",
        required=True,
        choices=["git-username", "random", "custom"],
        help="approved fixed-choice username mode",
    )
    apply.add_argument(
        "--custom-username",
        help="required only when --choice custom; letters and digits only",
    )
    apply.set_defaults(func=cmd_apply)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WorkspaceStateError as exc:
        print_json(status_payload("blocked", success=False, action="failed", message=str(exc)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
