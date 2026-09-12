#!/usr/bin/env python3
"""Run selected local tests with per-file/suite progress and retained logs."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_venv import ensure_workspace_interpreter

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_local_tests import main

if __name__ == "__main__":
    raise SystemExit(main(ROOT))
