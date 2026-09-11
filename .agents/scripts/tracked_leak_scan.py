#!/usr/bin/env python3
"""Scan tracked files, a staged diff, or a commit range for leaked identifiers.

Progress goes to stderr; one machine-readable JSON payload goes to stdout, per
the repo's script convention.

Examples:

    python3 .agents/scripts/tracked_leak_scan.py
    python3 .agents/scripts/tracked_leak_scan.py --staged
    python3 .agents/scripts/tracked_leak_scan.py --commit-range origin/main..HEAD
    python3 .agents/scripts/tracked_leak_scan.py --paths docs/tracked-leak-guard.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_leak_guard import (  # noqa: E402
    CATEGORIES,
    LeakGuardError,
    Policy,
    ScanResult,
    default_policy_file,
    load_policy,
    range_diff,
    scan_diff,
    scan_files,
    staged_diff,
    tracked_files,
    unused_entry_ids,
    require_knowledge_redact,
)


def emit_progress(message: str) -> None:
    print(f"[tracked-leak-scan] {message}", file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="repository root to scan; defaults to the repo containing this script",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--staged",
        action="store_true",
        help="scan only the added lines of the staged diff (pre-commit mode)",
    )
    mode.add_argument(
        "--commit-range",
        help="scan only the added lines of a commit range, e.g. origin/main..HEAD",
    )
    mode.add_argument(
        "--paths",
        nargs="+",
        help="scan these repository-relative paths instead of the whole tracked tree",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "policy/allowlist file; defaults to "
            "<repo-root>/.agents/leak-guard/allowlist.yaml"
        ),
    )
    parser.add_argument(
        "--no-allowlist",
        action="store_true",
        help="ignore the policy file entirely (built-in allowed ranges still apply)",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=sorted(CATEGORIES),
        help="restrict reporting to these categories",
    )
    parser.add_argument(
        "--show-matches",
        action="store_true",
        help="print full matched text instead of a redacted preview (local use only)",
    )
    parser.add_argument(
        "--strict-allowlist",
        action="store_true",
        help="also fail when a policy allowlist entry matched nothing",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="text",
        help="text prints findings on stderr plus compact JSON on stdout; json prints indented JSON only",
    )
    return parser


def _filter(result: ScanResult, categories: list[str] | None) -> ScanResult:
    if not categories:
        return result
    keep = set(categories)
    result.findings = [item for item in result.findings if item.category in keep]
    result.suppressed = [item for item in result.suppressed if item.category in keep]
    return result


def run(args: argparse.Namespace) -> tuple[dict, int]:
    require_knowledge_redact()
    repo_root = args.repo_root.resolve()
    if args.no_allowlist:
        policy_path = None
    elif args.allowlist is not None:
        policy_path = args.allowlist
    else:
        policy_path = default_policy_file(repo_root)
    policy: Policy = load_policy(policy_path)
    emit_progress(
        "policy: "
        + (
            f"{policy.source} "
            f"({len(policy.entries)} allowlist entries, "
            f"{len(policy.scoped_exclusions)} scoped exclusions)"
            if policy.source
            else "built-in defaults only"
        )
    )

    if args.staged:
        mode = "staged"
        emit_progress("reading staged diff")
        result = scan_diff(staged_diff(repo_root), policy)
    elif args.commit_range:
        mode = "commit-range"
        emit_progress(f"reading diff for {args.commit_range}")
        result = scan_diff(range_diff(repo_root, args.commit_range), policy)
    else:
        if args.paths:
            mode = "paths"
            paths = [Path(item).as_posix() for item in args.paths]
        else:
            mode = "tracked-tree"
            emit_progress("listing tracked files (submodule gitlinks excluded)")
            paths = tracked_files(repo_root)
        emit_progress(f"scanning {len(paths)} files")
        result = scan_files(repo_root, paths, policy, progress=emit_progress)

    result = _filter(result, args.categories)
    stale = unused_entry_ids(policy) if mode == "tracked-tree" else []
    failed = bool(result.findings) or (args.strict_allowlist and bool(stale))
    payload = {
        "schema_version": 1,
        "status": "failed" if failed else "passed",
        "mode": mode,
        "repo_root": str(repo_root),
        "policy_file": str(policy.source) if policy.source else None,
        "scanned_file_count": result.scanned,
        "skipped_file_count": len(result.skipped),
        "skipped": result.skipped[:50],
        "finding_count": len(result.findings),
        "findings": [item.to_dict(show_matches=args.show_matches) for item in result.findings],
        "suppressed_count": len(result.suppressed),
        "suppressed": [item.to_dict(show_matches=args.show_matches) for item in result.suppressed],
        "unused_allowlist_entries": stale,
        "categories": sorted(args.categories or CATEGORIES),
    }
    emit_progress(
        f"done: {payload['finding_count']} findings, "
        f"{payload['suppressed_count']} allowlisted, "
        f"{payload['skipped_file_count']} files skipped"
    )
    return payload, 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        payload, code = run(args)
    except LeakGuardError as exc:
        print(json.dumps({"schema_version": 1, "status": "error", "error": str(exc)}))
        emit_progress(f"error: {exc}")
        return 2
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return code
    for finding in payload["findings"]:
        print(
            f"{finding['path']}:{finding['line']}:{finding['column']}: "
            f"{finding['category']} ({finding['rule']}): {finding['preview']}",
            file=sys.stderr,
        )
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
