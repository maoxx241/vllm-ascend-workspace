#!/usr/bin/env python3
"""Thin CLI over ``vaws_coordinator.parity``.

Command-line surface and stdout contract are unchanged so sibling scripts
(``parity_sync.py``, ``parity_watch.py``, ``gc_runtime_cache.py``), skill
tests, and the CLI-surface inventory keep working. The implementation lives
in the package; this file re-exports it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_coordinator.parity import *  # noqa: E402,F403
from vaws_coordinator.parity import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
