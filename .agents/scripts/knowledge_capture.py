#!/usr/bin/env python3
"""Capture or merge one verified workspace knowledge candidate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_knowledge import (  # noqa: E402
    KnowledgeError,
    capture_candidate,
    knowledge_session_key,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--defer",
        action="store_true",
        help="stage the candidate for the current SessionEnd hook",
    )
    parser.add_argument(
        "--session-id",
        help="session identity for --defer; defaults to CODEX_THREAD_ID or source.session_id",
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "candidates",
    )
    parser.add_argument(
        "--pending-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "pending",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise KnowledgeError("input root must be an object")
        candidate_dir = args.candidate_dir
        if args.defer:
            source = payload.setdefault("source", {})
            if not isinstance(source, dict):
                raise KnowledgeError("source must be an object")
            session_id = (
                args.session_id
                or os.environ.get("CODEX_THREAD_ID")
                or source.get("session_id")
            )
            if not session_id:
                raise KnowledgeError(
                    "--defer requires --session-id, CODEX_THREAD_ID, or source.session_id"
                )
            source["session_id"] = session_id
            candidate_dir = args.pending_dir / knowledge_session_key(session_id)
        result = capture_candidate(
            payload,
            candidate_dir=candidate_dir,
            knowledge_dir=args.knowledge_dir,
        )
        if args.defer and result["status"] != "already-promoted":
            result["deferred"] = True
            result["session_key"] = knowledge_session_key(source["session_id"])
    except (OSError, json.JSONDecodeError, KnowledgeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
