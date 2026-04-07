#!/usr/bin/env python3
"""Shared machine-username choice helpers for repo-init.

This module keeps the repo-init machine-profile question narrow and stable.
The public repo-init flow should present exactly three options:
- use the detected Git / GitHub username
- generate a random ``agent#####`` username
- let the user provide a custom username
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

LIB_DIR = pathlib.Path(__file__).resolve().parents[3] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import (  # noqa: E402
    WorkspaceStateError,
    generate_machine_username,
    validate_machine_username,
)

REMOTE_PATTERNS = [
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
    r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
]


def run(cmd: list[str], cwd: pathlib.Path | None = None) -> tuple[int, str, str]:
    if cmd and cmd[0] == "git":
        cmd = ["git", "-c", "safe.directory=*", *cmd[1:]]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def which(name: str) -> str | None:
    return shutil.which(name)


def parse_remote_url(url: str) -> str | None:
    for pattern in REMOTE_PATTERNS:
        match = re.match(pattern, url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def _normalize_candidate(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return validate_machine_username(value)
    except WorkspaceStateError:
        return None


def _candidate_from_gh() -> dict[str, Any] | None:
    if which("gh") is None:
        return None
    rc, _, _ = run(["gh", "auth", "status", "--hostname", "github.com"])
    if rc != 0:
        return None
    rc, login, _ = run(["gh", "api", "user", "--jq", ".login"])
    if rc != 0:
        return None
    candidate = _normalize_candidate(login)
    if candidate is None:
        return None
    return {
        "available": True,
        "candidate": candidate,
        "source": "gh-user-login",
        "raw_value": login,
    }


def _candidate_from_origin(cwd: pathlib.Path | None = None) -> dict[str, Any] | None:
    rc, origin_url, _ = run(["git", "remote", "get-url", "origin"], cwd=cwd)
    if rc != 0 or not origin_url:
        return None
    repo = parse_remote_url(origin_url)
    if repo is None:
        return None
    owner, _, _ = repo.partition("/")
    candidate = _normalize_candidate(owner)
    if candidate is None:
        return None
    return {
        "available": True,
        "candidate": candidate,
        "source": "origin-owner",
        "raw_value": owner,
        "origin_repo": repo,
    }


def _candidate_from_git_config(cwd: pathlib.Path | None = None) -> dict[str, Any] | None:
    rc, user_name, _ = run(["git", "config", "--get", "user.name"], cwd=cwd)
    if rc == 0 and user_name:
        candidate = _normalize_candidate(user_name)
        if candidate is not None:
            return {
                "available": True,
                "candidate": candidate,
                "source": "git-config-user-name",
                "raw_value": user_name,
            }

    rc, user_email, _ = run(["git", "config", "--get", "user.email"], cwd=cwd)
    if rc == 0 and user_email:
        local_part = user_email.split("@", 1)[0]
        candidate = _normalize_candidate(local_part)
        if candidate is not None:
            return {
                "available": True,
                "candidate": candidate,
                "source": "git-config-user-email-local-part",
                "raw_value": user_email,
            }
    return None


def detect_git_username_candidate(cwd: pathlib.Path | None = None) -> dict[str, Any]:
    for resolver in (_candidate_from_gh, lambda: _candidate_from_origin(cwd), lambda: _candidate_from_git_config(cwd)):
        result = resolver()
        if result is not None:
            return result
    return {
        "available": False,
        "candidate": None,
        "source": None,
        "raw_value": None,
    }


def random_username_preview() -> str:
    return generate_machine_username(prefix="agent", suffix_length=5)


def fixed_machine_username_question(cwd: pathlib.Path | None = None) -> dict[str, Any]:
    git_username = detect_git_username_candidate(cwd)
    random_preview = random_username_preview()

    git_description = (
        f"使用当前检测到的 Git 用户名 {git_username['candidate']}"
        if git_username["available"]
        else "当前没有检测到可用的 Git 用户名；若选择此项，必须先让 agent 修复或确认 Git / GitHub 身份"
    )

    return {
        "question": "你希望这台开发机使用哪个机器用户名？",
        "mode": "single-choice",
        "rules": "3-32 chars, lowercase English letters and digits only",
        "fixed_options_only": True,
        "followup_required_for": ["custom"],
        "followup_question": "请输入你想使用的机器用户名（仅限英文和数字，3-32 位）",
        "options": [
            {
                "id": "git-username",
                "label": (
                    f"使用当前 Git 用户名（{git_username['candidate']}）"
                    if git_username["available"]
                    else "使用当前 Git 用户名"
                ),
                "description": git_description,
                "available": git_username["available"],
                "candidate": git_username["candidate"],
                "source": git_username["source"],
            },
            {
                "id": "random",
                "label": f"随机生成（例如 {random_preview}）",
                "description": "生成 agent + 5 位随机数字的用户名，实际值在创建时确定",
                "available": True,
                "pattern": "agent#####",
            },
            {
                "id": "custom",
                "label": "自定义",
                "description": "用户输入什么就用什么；仅接受英文字母和数字。选择此项后必须再问一次具体用户名。",
                "available": True,
                "requires_followup_text": True,
            },
        ],
    }
