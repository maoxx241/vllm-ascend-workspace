#!/usr/bin/env python3
"""Thin native final-response adapter over the installed knowledge package."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        from vaws_knowledge.summary_hook import capture_summary
        from vaws_knowledge_service import service_config

        raw = sys.stdin.read(1_048_577)
        if len(raw) <= 1_048_576:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                capture_summary(payload, config=service_config(ROOT), client=args.client)
    except Exception as exc:
        print(f"Knowledge summary capture deferred: {type(exc).__name__}", file=sys.stderr)
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
