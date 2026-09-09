#!/usr/bin/env python3
"""Start vLLM service by spawning serve_start.py."""
from __future__ import annotations

import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=LIB_DIR.parent.parent)

from vaws_remote_adapters import cli_service_start  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(cli_service_start())
