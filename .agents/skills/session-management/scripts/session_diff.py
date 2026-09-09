#!/usr/bin/env python3
"""Summarize local changes of this VAWS task's bound source worktrees.

Identity comes from --context-file / VAWS_CONTEXT_FILE. This does not create
containers or worktrees.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_result_envelope import emit_skill_json  # noqa: E402
from vaws_task_target import task_client, task_id_of  # noqa: E402


def print_json(data: dict[str, Any]) -> None:
    emit_skill_json(
        data,
        skill="session-management",
        entry_point=".agents/skills/session-management/scripts/session_diff.py",
    )


def run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in proc.stdout.splitlines() if line.strip()]


def repo_changes(repo: Path, base: str | None, *, with_stat: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(repo),
        "branch": run_git(["branch", "--show-current"], cwd=repo).stdout.strip() or "(detached)",
        "head": run_git(["rev-parse", "--short", "HEAD"], cwd=repo).stdout.strip(),
        "base": base,
    }
    payload["uncommitted"] = _lines(run_git(["status", "--porcelain"], cwd=repo))
    if base:
        base_ok = run_git(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=repo).returncode == 0
        if not base_ok:
            payload["base_error"] = f"base ref/commit {base!r} not found in {repo}"
            return payload
        payload["commits"] = _lines(run_git(["log", "--oneline", f"{base}..HEAD"], cwd=repo))
        payload["changed_files"] = _lines(run_git(["diff", "--name-status", f"{base}...HEAD"], cwd=repo))
        if with_stat:
            payload["diffstat"] = run_git(["diff", "--stat", f"{base}...HEAD"], cwd=repo).stdout
    else:
        payload["commits"] = []
        payload["changed_files"] = []
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--context-file", help="VAWS task context; defaults to VAWS_CONTEXT_FILE")
    parser.add_argument("--stat", action="store_true", help="include full diffstat text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = task_client(args.context_file)
        status = client.status()
        task_id = task_id_of(client)
        sources = (status.get("session") or {}).get("sources") or {}
        repos: list[dict[str, Any]] = []
        has_changes = False
        for name, source in sources.items():
            path = Path(source["path"] if isinstance(source, dict) else source)
            base = source.get("head_at_bind") if isinstance(source, dict) else None
            change = repo_changes(path, base, with_stat=args.stat)
            change["name"] = name
            repos.append(change)
            if change.get("uncommitted") or change.get("commits") or change.get("changed_files"):
                has_changes = True
        print_json({
            "status": "ok",
            "task_id": task_id,
            "has_changes": has_changes,
            "sources": repos,
        })
        return 0
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
