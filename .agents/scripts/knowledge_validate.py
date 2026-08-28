#!/usr/bin/env python3
"""Validate all shared workspace knowledge documents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_knowledge import KnowledgeError, validate_knowledge_dir  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    args = parser.parse_args(argv)
    try:
        files = validate_knowledge_dir(args.knowledge_dir)
    except KnowledgeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": "passed",
                "knowledge_dir": str(args.knowledge_dir.resolve()),
                "files": files,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
