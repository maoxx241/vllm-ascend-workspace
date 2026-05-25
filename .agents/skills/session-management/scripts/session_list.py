#!/usr/bin/env python3
"""List VAWS sessions and local resource leases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_session_state import load_index, load_leases, load_session_lookup  # noqa: E402


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="omit removed and stopped sessions from the listing",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        index = load_index(ROOT)
        sessions: list[dict[str, Any]] = []
        for sid, record in sorted(index.get("sessions", {}).items()):
            entry = dict(record)
            try:
                lookup = load_session_lookup(session_id=sid, repo_root=ROOT)
                session = lookup.session
                entry.update(
                    {
                        "status": session.get("status"),
                        "worktree_root": session.get("local", {}).get("worktree_root"),
                        "container": session.get("remote", {}).get("container"),
                        "leases": session.get("leases"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                entry["load_error"] = str(exc)
            if args.active_only and entry.get("status") in {"removed", "stopped"}:
                continue
            sessions.append(entry)
        print_json(
            {
                "status": "ok",
                "count": len(sessions),
                "sessions": sessions,
                "leases": load_leases(ROOT),
            }
        )
        return 0
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
