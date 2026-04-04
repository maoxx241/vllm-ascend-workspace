#!/usr/bin/env python3
"""Read-only probe for repo-init.

The script reports machine state, GitHub CLI state, GitHub auth state,
recursive submodule state, and local remote topology for:
  - workspace
  - vllm
  - vllm-ascend

It never mutates the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

COMMUNITY = {
    "workspace": "maoxx241/vllm-ascend-workspace",
    "vllm": "vllm-project/vllm",
    "vllm-ascend": "vllm-project/vllm-ascend",
}

REPO_PATHS = {
    "workspace": ".",
    "vllm": "vllm",
    "vllm-ascend": "vllm-ascend",
}


def run(cmd: List[str], cwd: Optional[pathlib.Path] = None) -> Tuple[int, str, str]:
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


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def detect_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    release = platform.release().lower()
    if "microsoft" in release or "wsl" in release:
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def os_release() -> Dict[str, str]:
    data: Dict[str, str] = {}
    if platform.system() != "Linux":
        return data
    path = pathlib.Path("/etc/os-release")
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    return data


def detect_platform() -> Dict[str, Any]:
    system = platform.system()
    release_info = os_release()
    if system == "Darwin":
        kind = "macos"
    elif system == "Windows":
        kind = "windows"
    elif detect_wsl():
        kind = "wsl"
    else:
        kind = "linux"

    return {
        "kind": kind,
        "system": system,
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "os_release": release_info,
    }


def parse_remote_url(url: str) -> Optional[str]:
    patterns = [
        r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def git_root() -> Optional[pathlib.Path]:
    rc, out, _ = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0 or not out:
        return None
    return pathlib.Path(out).resolve()


def git_submodule_status(root: pathlib.Path) -> List[Dict[str, str]]:
    rc, out, err = run(["git", "submodule", "status", "--recursive"], cwd=root)
    if rc != 0:
        return [{"error": err or "unable to inspect submodules"}]
    rows: List[Dict[str, str]] = []
    for line in out.splitlines():
        if not line:
            continue
        state = line[0]
        parts = line[1:].strip().split()
        if len(parts) < 2:
            rows.append({"raw": line})
            continue
        commit, path = parts[0], parts[1]
        branch = parts[2] if len(parts) > 2 else ""
        rows.append(
            {
                "state": state,
                "commit": commit,
                "path": path,
                "detail": branch,
            }
        )
    return rows


def branch_info(cwd: pathlib.Path) -> Dict[str, Any]:
    rc, branch, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    detached = branch == "HEAD" or rc != 0
    rc2, head, _ = run(["git", "rev-parse", "HEAD"], cwd=cwd)
    rc3, tracking, _ = run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=cwd,
    )
    ahead = behind = None
    if rc3 == 0:
        rc4, counts, _ = run(["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"], cwd=cwd)
        if rc4 == 0 and counts:
            left, right = counts.split()
            ahead, behind = int(left), int(right)
    return {
        "current_branch": None if detached else branch,
        "detached_head": detached,
        "head_commit": head if rc2 == 0 else None,
        "tracking_branch": tracking if rc3 == 0 else None,
        "ahead_of_tracking": ahead,
        "behind_tracking": behind,
    }


def git_dirty(cwd: pathlib.Path) -> Dict[str, Any]:
    rc, out, err = run(["git", "status", "--porcelain"], cwd=cwd)
    if rc != 0:
        return {"error": err or "unable to inspect worktree"}
    rows = out.splitlines()
    return {
        "dirty": bool(rows),
        "entries": rows[:50],
        "entry_count": len(rows),
    }


def remotes(cwd: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    rc, out, err = run(["git", "remote"], cwd=cwd)
    if rc != 0:
        return {"error": {"message": err or "unable to inspect remotes"}}
    names = [line.strip() for line in out.splitlines() if line.strip()]
    data: Dict[str, Dict[str, Any]] = {}
    for name in names:
        fetch_rc, fetch_url, _ = run(["git", "remote", "get-url", name], cwd=cwd)
        push_rc, push_url, _ = run(["git", "remote", "get-url", "--push", name], cwd=cwd)
        fetch_value = fetch_url if fetch_rc == 0 else None
        push_value = push_url if push_rc == 0 else None
        data[name] = {
            "fetch_url": fetch_value,
            "push_url": push_value,
            "fetch_repo": parse_remote_url(fetch_value) if fetch_value else None,
            "push_repo": parse_remote_url(push_value) if push_value else None,
        }
    return data


def classify_remote(
    repo_role: str,
    full_name: Optional[str],
    user_login: Optional[str],
) -> str:
    if not full_name:
        return "missing"
    if full_name == COMMUNITY[repo_role]:
        return "community"
    repo_basename = COMMUNITY[repo_role].split("/", 1)[1]
    if user_login and full_name == f"{user_login}/{repo_basename}":
        return "user-fork"
    return "other"


def inspect_repo(
    root: pathlib.Path,
    repo_role: str,
    user_login: Optional[str],
) -> Dict[str, Any]:
    repo_path = (root / REPO_PATHS[repo_role]).resolve()
    result: Dict[str, Any] = {
        "path": str(repo_path),
        "exists": repo_path.exists(),
        "initialized": False,
    }
    if not repo_path.exists():
        return result

    rc, _, err = run(["git", "rev-parse", "--git-dir"], cwd=repo_path)
    if rc != 0:
        result["error"] = err or "not a git repository"
        return result

    rc_top, top, top_err = run(["git", "rev-parse", "--show-toplevel"], cwd=repo_path)
    if rc_top != 0 or pathlib.Path(top).resolve() != repo_path:
        result["error"] = top_err or "path is inside a parent repository but is not an initialized submodule"
        return result

    result["initialized"] = True
    result["branch"] = branch_info(repo_path)
    result["worktree"] = git_dirty(repo_path)
    remote_data = remotes(repo_path)
    result["remotes"] = remote_data

    if "error" not in remote_data:
        origin_fetch = remote_data.get("origin", {}).get("fetch_repo")
        upstream_fetch = remote_data.get("upstream", {}).get("fetch_repo")
        result["origin_kind"] = classify_remote(repo_role, origin_fetch, user_login)
        result["upstream_kind"] = classify_remote(repo_role, upstream_fetch, user_login)

    return result


def gh_login() -> Dict[str, Any]:
    gh_path = which("gh")
    if not gh_path:
        return {"installed": False}

    rc, out, err = run(["gh", "auth", "status", "--hostname", "github.com"])
    status = {
        "installed": True,
        "path": gh_path,
        "logged_in": rc == 0,
        "auth_status_stdout": out,
        "auth_status_stderr": err,
    }
    if rc != 0:
        return status

    rc2, login, _ = run(["gh", "api", "user", "--jq", ".login"])
    if rc2 == 0 and login:
        status["user_login"] = login

    rc3, protocol, _ = run(["gh", "config", "get", "git_protocol", "--host", "github.com"])
    if rc3 == 0 and protocol:
        status["git_protocol"] = protocol

    return status


def gh_fork_info(user_login: Optional[str]) -> Dict[str, Any]:
    if not user_login or not which("gh"):
        return {}

    info: Dict[str, Any] = {}
    for role, community in COMMUNITY.items():
        repo_name = community.split("/", 1)[1]
        full_name = f"{user_login}/{repo_name}"
        rc, out, err = run(["gh", "api", f"repos/{full_name}"])
        if rc != 0:
            info[role] = {"exists": False, "error": err}
            continue
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            info[role] = {"exists": True, "error": "unable to decode gh api output"}
            continue

        parent = payload.get("parent") or {}
        info[role] = {
            "exists": True,
            "full_name": payload.get("full_name"),
            "is_fork": bool(payload.get("fork")),
            "parent_full_name": parent.get("full_name"),
            "default_branch": payload.get("default_branch"),
            "ssh_url": payload.get("ssh_url"),
            "clone_url": payload.get("clone_url"),
        }
    return info


def gh_install_plan(platform_info: Dict[str, Any]) -> Dict[str, Any]:
    kind = platform_info["kind"]
    has_brew = bool(which("brew"))
    has_apt = bool(which("apt"))
    has_winget = bool(which("winget"))

    if kind == "macos":
        if has_brew:
            preferred = {
                "label": "Homebrew",
                "commands": ["brew install gh"],
                "requires_privilege": False,
            }
        else:
            preferred = {
                "label": "Homebrew bootstrap + Homebrew install",
                "commands": [
                    '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
                    "brew install gh",
                ],
                "requires_privilege": True,
            }
        fallback = {
            "label": "user-space installer",
            "commands": ["python3 .agents/skills/repo-init/scripts/install_gh_user.py"],
            "requires_privilege": False,
        }
    elif kind in {"linux", "wsl"} and has_apt:
        preferred = {
            "label": "official GitHub CLI Debian packages",
            "commands": [
                "(type -p wget >/dev/null || (sudo apt update && sudo apt install wget -y)) "
                "&& sudo mkdir -p -m 755 /etc/apt/keyrings "
                "&& out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg "
                "&& cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null "
                "&& sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg "
                "&& sudo mkdir -p -m 755 /etc/apt/sources.list.d "
                '&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] '
                'https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null '
                "&& sudo apt update && sudo apt install gh -y"
            ],
            "requires_privilege": True,
        }
        fallback = {
            "label": "user-space installer",
            "commands": ["python3 .agents/skills/repo-init/scripts/install_gh_user.py"],
            "requires_privilege": False,
        }
    elif kind == "windows" and has_winget:
        preferred = {
            "label": "WinGet",
            "commands": ["winget install --id GitHub.cli"],
            "requires_privilege": False,
        }
        fallback = {
            "label": "user-space installer",
            "commands": ["powershell -ExecutionPolicy Bypass -File .agents/skills/repo-init/scripts/install-gh-user.ps1"],
            "requires_privilege": False,
        }
    else:
        preferred = {
            "label": "user-space installer",
            "commands": [],
            "requires_privilege": False,
        }
        if kind == "windows":
            fallback = {
                "label": "user-space installer",
                "commands": ["powershell -ExecutionPolicy Bypass -File .agents/skills/repo-init/scripts/install-gh-user.ps1"],
                "requires_privilege": False,
            }
        else:
            fallback = {
                "label": "user-space installer",
                "commands": ["python3 .agents/skills/repo-init/scripts/install_gh_user.py"],
                "requires_privilege": False,
            }

    return {
        "preferred": preferred,
        "fallback": fallback,
    }


def tool_state() -> Dict[str, Optional[str]]:
    names = [
        "git",
        "gh",
        "ssh",
        "ssh-keygen",
        "brew",
        "apt",
        "winget",
        "sudo",
        "python3",
        "python",
        "pwsh",
        "powershell",
    ]
    return {name: which(name) for name in names}


def compact_repo_summary(repo: Dict[str, Any]) -> Dict[str, Any]:
    branch = repo.get("branch") or {}
    worktree = repo.get("worktree") or {}
    remotes = repo.get("remotes") or {}
    origin = remotes.get("origin") or {}
    upstream = remotes.get("upstream") or {}
    summary: Dict[str, Any] = {
        "path": repo.get("path"),
        "exists": repo.get("exists"),
        "initialized": repo.get("initialized"),
    }
    if repo.get("error"):
        summary["error"] = repo.get("error")
        return summary

    summary.update(
        {
            "current_branch": branch.get("current_branch"),
            "tracking_branch": branch.get("tracking_branch"),
            "detached_head": branch.get("detached_head"),
            "dirty": worktree.get("dirty"),
            "dirty_entries": worktree.get("entry_count"),
            "origin_repo": origin.get("fetch_repo"),
            "origin_kind": repo.get("origin_kind"),
            "upstream_repo": upstream.get("fetch_repo"),
            "upstream_kind": repo.get("upstream_kind"),
        }
    )
    return summary


def compact_submodule_summary(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    issues: List[Dict[str, str]] = []
    for row in rows:
        state = row.get("state")
        if state and state != " ":
            issues.append(
                {
                    "path": row.get("path", ""),
                    "state": state,
                    "detail": row.get("detail", ""),
                }
            )
        if row.get("error"):
            issues.append({"error": row["error"]})
    return {
        "count": len(rows),
        "needs_attention": issues,
    }


def compact_fork_summary(forks: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for role, info in forks.items():
        summary[role] = {
            "exists": info.get("exists"),
            "full_name": info.get("full_name"),
            "parent_full_name": info.get("parent_full_name"),
            "default_branch": info.get("default_branch"),
        }
    return summary


def compact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    gh = payload.get("gh") or {}
    compact: Dict[str, Any] = {
        "platform": {
            "kind": payload.get("platform", {}).get("kind"),
            "machine": payload.get("platform", {}).get("machine"),
        },
        "repo_root": payload.get("repo_root"),
        "gh": {
            "installed": gh.get("installed"),
            "logged_in": gh.get("logged_in"),
            "user_login": gh.get("user_login"),
            "git_protocol": gh.get("git_protocol"),
        },
        "gh_install_plan": {
            "preferred": payload.get("gh_install_plan", {}).get("preferred", {}).get("label"),
            "fallback": payload.get("gh_install_plan", {}).get("fallback", {}).get("label"),
        },
        "submodules": compact_submodule_summary(payload.get("submodules") or []),
        "repos": {
            role: compact_repo_summary(repo)
            for role, repo in (payload.get("repos") or {}).items()
        },
        "forks": compact_fork_summary(payload.get("forks") or {}),
    }
    return compact


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only probe for repo-init")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="print a compact summary instead of the full raw payload",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    platform_info = detect_platform()
    root = git_root()
    gh_state = gh_login()
    user_login = gh_state.get("user_login")

    payload: Dict[str, Any] = {
        "platform": platform_info,
        "tools": tool_state(),
        "gh": gh_state,
        "gh_install_plan": gh_install_plan(platform_info),
        "repo_root": str(root) if root else None,
        "cwd": str(pathlib.Path.cwd().resolve()),
    }

    if root:
        payload["submodules"] = git_submodule_status(root)
        payload["repos"] = {
            role: inspect_repo(root, role, user_login)
            for role in ("workspace", "vllm", "vllm-ascend")
        }
    else:
        payload["submodules"] = []
        payload["repos"] = {}

    if gh_state.get("logged_in") and user_login:
        payload["forks"] = gh_fork_info(user_login)
    else:
        payload["forks"] = {}

    if args.compact:
        payload = compact_payload(payload)

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
