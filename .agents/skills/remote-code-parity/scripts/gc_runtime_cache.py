#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from common import json_dump, now_utc


@dataclass
class Candidate:
    path: Path
    category: str
    modified_ns: int


def list_candidates(workspace_root: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    for category in ("manifests", "logs"):
        category_path = workspace_root / category
        if not category_path.exists():
            continue
        for item in category_path.iterdir():
            if item.is_file():
                candidates.append(Candidate(item, category, item.stat().st_mtime_ns))
    return sorted(candidates, key=lambda item: item.modified_ns, reverse=True)


def run_gc(args: argparse.Namespace) -> int:
    workspace_root = Path(args.storage_root) / "remote-code-parity" / "workspaces" / args.workspace_id
    candidates = list_candidates(workspace_root)

    kept: list[str] = []
    removed: list[str] = []
    keep_limit = {
        "manifests": args.keep_success,
        "logs": args.keep_failure,
    }
    counts = {"manifests": 0, "logs": 0}
    for candidate in candidates:
        counts[candidate.category] += 1
        if counts[candidate.category] <= keep_limit[candidate.category]:
            kept.append(str(candidate.path))
            continue
        removed.append(str(candidate.path))
        if not args.dry_run:
            candidate.path.unlink(missing_ok=True)

    print(
        json_dump(
            {
                "generated_at": now_utc(),
                "workspace_root": str(workspace_root),
                "dry_run": args.dry_run,
                "kept": kept,
                "removed": removed,
            }
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prune old remote-code-parity manifests and logs.")
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--keep-success", type=int, default=3)
    parser.add_argument("--keep-failure", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    return run_gc(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
