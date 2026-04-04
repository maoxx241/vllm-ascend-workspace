#!/usr/bin/env python3
"""Deterministic helper for repo-init remote topology.

This script keeps common git-remote mutations compact and predictable so the
agent does not need to construct noisy ad-hoc shell pipelines.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from typing import Any, Sequence


class RepoTopologyError(RuntimeError):
    """Raised for deterministic, user-facing failures."""


GIT_URL_PATTERNS = [
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
    r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
]


def run(
    cmd: Sequence[str],
    *,
    cwd: pathlib.Path | None = None,
    check: bool = False,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "command failed"
        raise RepoTopologyError(detail)
    if quiet:
        return proc
    return proc


def parse_repo_url(url: str | None) -> str | None:
    if not url:
        return None
    for pattern in GIT_URL_PATTERNS:
        match = re.match(pattern, url)
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def resolve_repo(path_value: str) -> pathlib.Path:
    path = pathlib.Path(path_value).expanduser().resolve()
    proc = run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RepoTopologyError(f"not a git repository: {path}")
    return pathlib.Path(proc.stdout.strip()).resolve()


def branch_exists(repo: pathlib.Path, branch: str) -> bool:
    return run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo).returncode == 0


def local_head(repo: pathlib.Path, ref: str) -> str | None:
    proc = run(["git", "rev-parse", ref], cwd=repo)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def remote_names(repo: pathlib.Path) -> list[str]:
    proc = run(["git", "remote"], cwd=repo, check=True)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def remote_url(repo: pathlib.Path, name: str, *, push: bool = False) -> str | None:
    cmd = ["git", "remote", "get-url"]
    if push:
        cmd.append("--push")
    cmd.append(name)
    proc = run(cmd, cwd=repo)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def local_branch_status(repo: pathlib.Path) -> dict[str, Any]:
    branch_proc = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else None
    detached = branch in {None, "", "HEAD"}

    upstream_proc = run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=repo,
    )
    upstream = upstream_proc.stdout.strip() if upstream_proc.returncode == 0 else None

    dirty_proc = run(["git", "status", "--porcelain"], cwd=repo, check=True)
    dirty_rows = [line for line in dirty_proc.stdout.splitlines() if line]

    return {
        "current_branch": None if detached else branch,
        "detached_head": detached,
        "tracking_branch": upstream,
        "dirty": bool(dirty_rows),
        "dirty_entries": dirty_rows[:20],
        "dirty_entry_count": len(dirty_rows),
        "head": local_head(repo, "HEAD"),
    }


def cmd_compare_main(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    remotes = remote_names(repo)
    branch = args.branch
    result: dict[str, Any] = {
        "repo": str(repo),
        "branch": branch,
        "local_branch": local_head(repo, f"refs/heads/{branch}"),
        "status": local_branch_status(repo),
        "remotes": {},
    }

    for name in remotes:
        ls = run(["git", "ls-remote", "--heads", name, branch], cwd=repo)
        remote_head = None
        if ls.returncode == 0 and ls.stdout.strip():
            remote_head = ls.stdout.split()[0]
        tracking_head = local_head(repo, f"refs/remotes/{name}/{branch}")
        fetch_url = remote_url(repo, name)
        push_url = remote_url(repo, name, push=True)
        result["remotes"][name] = {
            "fetch_url": fetch_url,
            "push_url": push_url,
            "fetch_repo": parse_repo_url(fetch_url),
            "push_repo": parse_repo_url(push_url),
            "remote_head": remote_head,
            "tracking_head": tracking_head,
            "tracking_matches_remote": None if not (remote_head and tracking_head) else tracking_head == remote_head,
            "local_matches_remote": None if not (remote_head and result["local_branch"]) else result["local_branch"] == remote_head,
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def mutate_remote(repo: pathlib.Path, name: str, desired_url: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    existing_fetch = remote_url(repo, name)
    existing_push = remote_url(repo, name, push=True)
    if existing_fetch is None:
        run(["git", "remote", "add", name, desired_url], cwd=repo, check=True)
        actions.append({"remote": name, "action": "added", "url": desired_url})
        existing_fetch = remote_url(repo, name)
        existing_push = remote_url(repo, name, push=True)
    elif existing_fetch != desired_url:
        run(["git", "remote", "set-url", name, desired_url], cwd=repo, check=True)
        actions.append({"remote": name, "action": "set-fetch-url", "from": existing_fetch, "to": desired_url})
        existing_fetch = desired_url

    if existing_push not in {None, desired_url}:
        run(["git", "remote", "set-url", "--push", name, desired_url], cwd=repo, check=True)
        actions.append({"remote": name, "action": "set-push-url", "from": existing_push, "to": desired_url})
    elif existing_push is None:
        run(["git", "remote", "set-url", "--push", name, desired_url], cwd=repo, check=True)
        actions.append({"remote": name, "action": "set-push-url", "from": None, "to": desired_url})

    return actions


def cmd_configure(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    actions: list[dict[str, Any]] = []

    if args.origin_url:
        actions.extend(mutate_remote(repo, "origin", args.origin_url))
    if args.upstream_url:
        actions.extend(mutate_remote(repo, "upstream", args.upstream_url))

    if args.gh_default and args.gh_default != "none":
        gh_proc = run(["gh", "repo", "set-default", args.gh_default], cwd=repo)
        if gh_proc.returncode != 0:
            actions.append(
                {
                    "action": "gh-default-failed",
                    "target": args.gh_default,
                    "detail": gh_proc.stderr.strip() or gh_proc.stdout.strip() or "gh command failed",
                }
            )
        else:
            actions.append({"action": "gh-default-set", "target": args.gh_default})

    result = {
        "repo": str(repo),
        "actions": actions,
        "remotes": {
            name: {
                "fetch_url": remote_url(repo, name),
                "push_url": remote_url(repo, name, push=True),
                "fetch_repo": parse_repo_url(remote_url(repo, name)),
                "push_repo": parse_repo_url(remote_url(repo, name, push=True)),
            }
            for name in remote_names(repo)
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_ensure_main(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    branch = args.branch
    remote = args.remote
    status_before = local_branch_status(repo)
    if status_before["dirty"] and not args.allow_dirty:
        raise RepoTopologyError(
            "worktree is dirty; rerun with --allow-dirty or clean the repository first"
        )

    run(
        [
            "git",
            "fetch",
            "--quiet",
            "--no-tags",
            remote,
            f"refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        ],
        cwd=repo,
        check=True,
    )

    actions: list[dict[str, Any]] = [
        {"action": "fetch-branch", "remote": remote, "branch": branch}
    ]

    if branch_exists(repo, branch):
        current = status_before["current_branch"]
        if current != branch:
            run(["git", "switch", branch], cwd=repo, check=True)
            actions.append({"action": "switch-existing-branch", "branch": branch})
    else:
        run(["git", "switch", "-c", branch, "--track", f"{remote}/{branch}"], cwd=repo, check=True)
        actions.append({"action": "create-tracking-branch", "branch": branch, "tracking": f"{remote}/{branch}"})

    run(["git", "branch", "--set-upstream-to", f"{remote}/{branch}", branch], cwd=repo, check=True)
    actions.append({"action": "set-upstream", "branch": branch, "tracking": f"{remote}/{branch}"})

    if args.pull:
        run(["git", "pull", "--ff-only", remote, branch], cwd=repo, check=True)
        actions.append({"action": "pull-ff-only", "remote": remote, "branch": branch})

    result = {
        "repo": str(repo),
        "actions": actions,
        "status": local_branch_status(repo),
        "local_branch": local_head(repo, f"refs/heads/{branch}"),
        "tracking_branch": local_head(repo, f"refs/remotes/{remote}/{branch}"),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    compare_main = subparsers.add_parser(
        "compare-main",
        help="compare local main/tracking refs with remote heads without broad fetch/prune",
    )
    compare_main.add_argument("--repo", required=True, help="repository path")
    compare_main.add_argument("--branch", default="main", help="branch to compare (default: main)")
    compare_main.set_defaults(func=cmd_compare_main)

    configure = subparsers.add_parser(
        "configure",
        help="configure origin/upstream conservatively and preserve extra remotes",
    )
    configure.add_argument("--repo", required=True, help="repository path")
    configure.add_argument("--origin-url")
    configure.add_argument("--upstream-url")
    configure.add_argument(
        "--gh-default",
        choices=["origin", "upstream", "none"],
        default="none",
        help="optionally run `gh repo set-default`",
    )
    configure.set_defaults(func=cmd_configure)

    ensure_main = subparsers.add_parser(
        "ensure-main",
        help="quietly fetch one branch, ensure a local tracking branch, and optionally pull",
    )
    ensure_main.add_argument("--repo", required=True, help="repository path")
    ensure_main.add_argument("--remote", required=True, help="tracking remote name")
    ensure_main.add_argument("--branch", default="main", help="branch name (default: main)")
    ensure_main.add_argument(
        "--allow-dirty",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="allow switching when the worktree is dirty",
    )
    ensure_main.add_argument(
        "--pull",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="run `git pull --ff-only` after setting tracking",
    )
    ensure_main.set_defaults(func=cmd_ensure_main)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RepoTopologyError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
