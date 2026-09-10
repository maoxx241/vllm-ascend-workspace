#!/usr/bin/env python3
"""Delegate Markdown contribution review to the installed knowledge package."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_knowledge.contribution.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
