#!/usr/bin/env python3
"""Print the ordinary endpoint for one coordinator execution."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=LIB_DIR.parent.parent)

from vaws_task_target import execution_target, task_client  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--context-file")
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args(argv)
    try:
        target = execution_target(task_client(args.context_file), args.execution_id)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(target, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
