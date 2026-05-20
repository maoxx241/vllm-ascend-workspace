"""Test fixtures: put ``scripts/`` on sys.path so test files can import
``_common`` and ``ascend_profile`` as top-level modules without installing
the package.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
