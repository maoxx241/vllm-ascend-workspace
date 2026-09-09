#!/usr/bin/env python3
"""Git `pre-commit` guard: block commits that add leaked identifiers.

Mirrors the guard pattern already used by `.remote-dev/hooks/`: detection lives
in a shared library (`.agents/lib/vaws_leak_guard.py`), and this file is the
thin client adapter for one host - here, Git's `pre-commit` event.

    python3 .agents/hooks/tracked_leak_precommit.py --install
    python3 .agents/hooks/tracked_leak_precommit.py --status
    python3 .agents/hooks/tracked_leak_precommit.py --uninstall

The check itself fails closed: a policy error, a missing policy file, an
unreadable staged diff, or a missing ``vaws-knowledge`` package blocks the
commit rather than passing it through. The remedy for the package gap is
``uv sync``.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402
from vaws_leak_guard import (  # noqa: E402
    DEFAULT_POLICY_PATH,
    LeakGuardError,
    ScanResult,
    default_policy_file,
    format_finding_line,
    load_policy,
    run_git,
    scan_diff,
    staged_diff,
)

HOOK_NAME = "pre-commit"
MARKER = "vaws-tracked-leak-guard"
SHIM = """#!/bin/sh
# {marker}: installed by .agents/hooks/tracked_leak_precommit.py
# Blocks commits that add addresses, home paths, e-mails, internal names, or
# credential-shaped strings to tracked files. See docs/tracked-leak-guard.md.
exec {python} "{hook}" --check
"""


def emit_progress(message: str) -> None:
    print(f"[tracked-leak-guard] {message}", file=sys.stderr, flush=True)


def hooks_dir(repo_root: Path) -> Path:
    """Resolve the shared hooks directory, honouring worktrees and hooksPath."""

    try:
        configured = str(
            run_git(["config", "--get", "core.hooksPath"], repo_root=repo_root)
        ).strip()
    except LeakGuardError:
        configured = ""  # `git config --get` exits 1 when the key is unset
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_absolute() else (repo_root / candidate).resolve()
    common_path = Path(
        str(run_git(["rev-parse", "--git-common-dir"], repo_root=repo_root)).strip()
    )
    if not common_path.is_absolute():
        common_path = (repo_root / common_path).resolve()
    return common_path / "hooks"


def resolve_policy_path(repo_root: Path, explicit: Path | None) -> Path:
    """Resolve the policy under the repository being committed to.

    The path is returned even when the file is missing so the loader can fail
    closed. An explicit `--allowlist` still wins. The installed shim is shared
    by linked worktrees; falling back to the installing tree would scan a
    foreign repository under the wrong policy.
    """

    if explicit is not None:
        return explicit
    return default_policy_file(repo_root)


def install(repo_root: Path, *, force: bool) -> dict:
    directory = hooks_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    hook_path = directory / HOOK_NAME
    backup: str | None = None
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        if MARKER in existing:
            action = "reinstalled"
        elif force:
            backup_path = hook_path.with_suffix(".pre-vaws-leak-guard")
            backup_path.write_text(existing, encoding="utf-8")
            backup = str(backup_path)
            action = "replaced"
        else:
            return {
                "status": "blocked",
                "action": "kept-existing-hook",
                "hook_path": str(hook_path),
                "error": (
                    "a different pre-commit hook is already installed; rerun with "
                    "--force to back it up and replace it, or chain this guard from it"
                ),
            }
    else:
        action = "installed"
    hook_path.write_text(
        SHIM.format(
            marker=MARKER,
            python=sys.executable or "python3",
            hook=Path(__file__).resolve(),
        ),
        encoding="utf-8",
    )
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return {
        "status": "passed",
        "action": action,
        "hook_path": str(hook_path),
        "backup_path": backup,
    }


def uninstall(repo_root: Path) -> dict:
    hook_path = hooks_dir(repo_root) / HOOK_NAME
    if not hook_path.exists():
        return {"status": "passed", "action": "absent", "hook_path": str(hook_path)}
    if MARKER not in hook_path.read_text(encoding="utf-8", errors="replace"):
        return {
            "status": "blocked",
            "action": "kept-foreign-hook",
            "hook_path": str(hook_path),
            "error": "pre-commit hook was not installed by this guard; leaving it untouched",
        }
    hook_path.unlink()
    return {"status": "passed", "action": "removed", "hook_path": str(hook_path)}


def status(repo_root: Path) -> dict:
    hook_path = hooks_dir(repo_root) / HOOK_NAME
    installed = hook_path.exists() and MARKER in hook_path.read_text(
        encoding="utf-8", errors="replace"
    )
    return {
        "status": "passed",
        "action": "status",
        "hook_path": str(hook_path),
        "installed": installed,
        "foreign_hook": hook_path.exists() and not installed,
        "policy_file": str(DEFAULT_POLICY_PATH),
    }


def check(repo_root: Path, *, policy_path: Path | None, show_matches: bool) -> dict:
    ensure_workspace_interpreter(repo_root=ROOT)
    policy_path = resolve_policy_path(repo_root, policy_path)
    policy = load_policy(policy_path)
    result: ScanResult = scan_diff(staged_diff(repo_root), policy)
    payload = {
        "status": "failed" if result.findings else "passed",
        "action": "check",
        "mode": "staged",
        "policy_file": str(policy.source) if policy.source else None,
        "scanned_file_count": result.scanned,
        "finding_count": len(result.findings),
        "findings": [item.to_dict(show_matches=show_matches) for item in result.findings],
        "suppressed_count": len(result.suppressed),
    }
    if result.findings:
        _report_block(result, policy_path, show_matches=show_matches)
    else:
        emit_progress(
            f"staged diff clean: {result.scanned} files, "
            f"{len(result.suppressed)} allowlisted matches"
        )
    return payload


def _report_block(result: ScanResult, policy_path: Path, *, show_matches: bool) -> None:
    lines = [
        "",
        "commit blocked: staged changes add identifiers that must not enter tracked files.",
        "",
    ]
    for finding in result.findings:
        lines.append("  " + format_finding_line(finding, show_matches=show_matches))
    categories = sorted({finding.category for finding in result.findings})
    example = result.findings[0]
    try:
        relative_policy = policy_path.relative_to(ROOT).as_posix()
    except ValueError:
        relative_policy = str(policy_path)
    lines += [
        "",
        f"categories: {', '.join(categories)}",
        "",
        "Next steps:",
        "  1. Replace the value. Use RFC 5737 (192.0.2.x), RFC 3849 (2001:db8::),",
        "     `example.invalid`, or a `<placeholder>` instead of a real address,",
        "     host, home directory, mailbox, or credential.",
        "  2. Inspect the full staged diff with:",
        "       python3 .agents/scripts/tracked_leak_scan.py --staged --show-matches",
        f"  3. If the value is genuinely safe, add an entry to {relative_policy}:",
        "",
        "       - id: <short-stable-id>",
        f"         path_glob: {example.path}",
        f"         categories: [{example.category}]",
        "         match: <the exact matched text>",
        "         justification: <why this value cannot identify a person, host, or secret>",
        "",
        "     A justification is mandatory and is reviewed with the diff. There is no",
        "     inline pragma. `git commit --no-verify` bypasses this hook, but CI runs",
        "     the same scanner over the tracked tree, so the finding will resurface.",
        "",
    ]
    sys.stderr.write("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="scan the staged diff (default)")
    mode.add_argument("--install", action="store_true", help="install the git pre-commit hook")
    mode.add_argument("--uninstall", action="store_true", help="remove the git pre-commit hook")
    mode.add_argument("--status", action="store_true", help="report hook installation state")
    parser.add_argument("--force", action="store_true", help="back up and replace a foreign hook")
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="policy file; defaults to .agents/leak-guard/allowlist.yaml of the committed repo",
    )
    parser.add_argument("--show-matches", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        if args.install:
            payload = install(repo_root, force=args.force)
        elif args.uninstall:
            payload = uninstall(repo_root)
        elif args.status:
            payload = status(repo_root)
        else:
            payload = check(
                repo_root, policy_path=args.allowlist, show_matches=args.show_matches
            )
    except LeakGuardError as exc:
        # Fail closed: an unreadable policy or diff must not silently allow a
        # commit through.
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        emit_progress(f"blocked: {exc}")
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
