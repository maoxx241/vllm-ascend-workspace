#!/usr/bin/env python3
"""Report the local machine username document. Host/container proof is coordinator-owned."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_local_state import profile_summary  # noqa: E402
from vaws_result_envelope import emit_skill_json  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__, allow_abbrev=False).parse_args(argv)
    try:
        emit_skill_json(
            {"status": "ok", "profile": profile_summary(), "note": "container SSH/NPU proof is coordinator-owned"},
            skill="machine-management",
            entry_point=".agents/skills/machine-management/scripts/machine_verify.py",
        )
        return 0
    except Exception as exc:
        emit_skill_json({"status": "failed", "error": str(exc)}, skill="machine-management", entry_point=".agents/skills/machine-management/scripts/machine_verify.py")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
