#!/usr/bin/env python3
"""Query compact workspace knowledge summaries or fetch one entry by id."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_knowledge import (  # noqa: E402
    KnowledgeError,
    get_knowledge_entry,
    query_knowledge,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--query")
    mode.add_argument("--id")
    parser.add_argument("--kind", action="append", dest="kinds")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--include-deprecated", action="store_true")
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    args = parser.parse_args(argv)
    try:
        if args.id:
            result = get_knowledge_entry(
                knowledge_dir=args.knowledge_dir, entry_id=args.id
            )
            payload = {
                "status": "passed" if result else "not-found",
                "id": args.id,
                "result": result,
            }
        else:
            matches = query_knowledge(
                knowledge_dir=args.knowledge_dir,
                query=args.query,
                kinds=args.kinds,
                limit=args.limit,
                include_deprecated=args.include_deprecated,
            )
            payload = {
                "status": "passed",
                "query": args.query,
                "matches": matches,
            }
    except KnowledgeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
