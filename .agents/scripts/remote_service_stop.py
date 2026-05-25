#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_remote_toolbox import cli_service_stop  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(cli_service_stop())
