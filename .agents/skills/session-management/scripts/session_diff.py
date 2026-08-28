#!/usr/bin/env python3
"""Summarize all local changes of a session: scaffold worktree + submodules.

One command answers "what did this agent session change?" for review:

    python3 session_diff.py                     # bound session (cwd upward)
    python3 session_diff.py --session-id <id>   # explicit session
    python3 session_diff.py --stat              # include full diffstat text

For the scaffold worktree the base is the session's recorded ``base_ref``.
For each submodule the base is the gitlink recorded at the session branch
creation time (``local.submodule_branches[].base_commit``), falling back to
the gitlink of ``base_ref`` in the scaffold repo.

Final JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_session_state import SessionStateError, load_session_lookup  # noqa: E402


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


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
    """Collect committed + uncommitted changes of one repo relative to ``base``."""
    payload: dict[str, Any] = {
        "path": str(repo),
        "branch": run_git(["branch", "--show-current"], cwd=repo).stdout.strip() or "(detached)",
        "head": run_git(["rev-parse", "--short", "HEAD"], cwd=repo).stdout.strip(),
        "base": base,
    }

    status = run_git(["status", "--porcelain"], cwd=repo)
    payload["uncommitted"] = _lines(status)

    if base:
        base_ok = run_git(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=repo).returncode == 0
        if not base_ok:
            payload["base_error"] = f"base ref/commit {base!r} not found in {repo}"
            return payload
        payload["commits"] = _lines(run_git(["log", "--oneline", f"{base}..HEAD"], cwd=repo))
        payload["changed_files"] = _lines(
            run_git(["diff", "--name-status", f"{base}...HEAD"], cwd=repo)
        )
        if with_stat:
            payload["diffstat"] = run_git(["diff", "--stat", f"{base}...HEAD"], cwd=repo).stdout.rstrip()
    return payload


def submodule_paths(worktree: Path) -> list[str]:
    proc = run_git(
        ["submodule", "foreach", "--quiet", "printf '%s\\n' \"$sm_path\""],
        cwd=worktree,
    )
    return _lines(proc)


def submodule_base(
    worktree: Path,
    rel_path: str,
    base_ref: str | None,
    recorded: dict[str, str],
) -> str | None:
    if rel_path in recorded:
        return recorded[rel_path]
    if not base_ref:
        return None
    proc = run_git(["rev-parse", f"{base_ref}:{rel_path}"], cwd=worktree)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--session-id", help="VAWS session id; defaults to the bound session of the current worktree")
    parser.add_argument("--session-file", help="explicit session.json path")
    parser.add_argument("--stat", action="store_true", help="include full diffstat text per repo")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        lookup = load_session_lookup(
            session_id=args.session_id,
            session_file=args.session_file,
            repo_root=ROOT,
        )
    except SessionStateError as exc:
        print_json({"status": "needs_input", "error": str(exc)})
        return 1

    session = lookup.session
    local = session.get("local", {})
    worktree = Path(local.get("worktree_root", str(ROOT))).expanduser().resolve()
    if not worktree.exists():
        print_json({
            "status": "failed",
            "session_id": session["session_id"],
            "error": f"worktree does not exist: {worktree}",
        })
        return 1

    base_ref = local.get("base_ref")
    recorded = {
        item["path"]: item["base_commit"]
        for item in local.get("submodule_branches", []) or []
        if isinstance(item, dict) and item.get("path") and item.get("base_commit")
    }

    scaffold = repo_changes(worktree, base_ref, with_stat=args.stat)
    submodules: list[dict[str, Any]] = []
    for rel_path in submodule_paths(worktree):
        sub_root = worktree / rel_path
        if not (sub_root / ".git").exists():
            submodules.append({"path": rel_path, "skipped": "not initialized"})
            continue
        base = submodule_base(worktree, rel_path, base_ref, recorded)
        item = repo_changes(sub_root, base, with_stat=args.stat)
        item["path"] = rel_path
        submodules.append(item)

    def _dirty(repo: dict[str, Any]) -> bool:
        return bool(repo.get("uncommitted") or repo.get("commits") or repo.get("changed_files"))

    print_json({
        "status": "ok",
        "session_id": session["session_id"],
        "worktree_root": str(worktree),
        "branch": local.get("branch"),
        "base_ref": base_ref,
        "has_changes": _dirty(scaffold) or any(_dirty(item) for item in submodules),
        "scaffold": scaffold,
        "submodules": submodules,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
