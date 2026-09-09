#!/usr/bin/env python3
"""Report unresolved coordinator executions. Never auto-delete or release.

Age, local PID death and missing local metadata are not release evidence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_result_envelope import emit_skill_json  # noqa: E402
from vaws_task_target import DONE, task_client, task_id_of  # noqa: E402


def print_json(data: dict[str, Any]) -> None:
    emit_skill_json(
        data,
        skill="session-management",
        entry_point=".agents/skills/session-management/scripts/session_gc.py",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--context-file", help="VAWS task context; defaults to VAWS_CONTEXT_FILE")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = task_client(args.context_file)
        status = client.status()
        executions = list(status.get("executions") or [])
        unresolved = [
            {
                "execution_id": row.get("id") or row.get("execution_id"),
                "state": row.get("phase") or row.get("state"),
                "service": (row.get("spec") or {}).get("service") or row.get("service"),
            }
            for row in executions
            if (row.get("phase") or row.get("state")) not in DONE
        ]
        print_json({
            "status": "ok",
            "task_id": task_id_of(client),
            "task_state": (status.get("session") or {}).get("state"),
            "unresolved": unresolved,
            "released": False,
            "deleted": False,
            "note": "GC reports unresolved ownership only; retry stop/finish after the condition clears",
        })
        return 0
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc), "released": False, "deleted": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
