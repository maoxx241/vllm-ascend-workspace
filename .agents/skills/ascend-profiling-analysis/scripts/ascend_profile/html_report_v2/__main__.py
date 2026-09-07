#!/usr/bin/env python3
"""``python -m ascend_profile.html_report_v2`` entry point."""
from __future__ import annotations

try:
    from ascend_profile.html_report_v2 import main  # type: ignore
except ImportError:  # pragma: no cover - script-mode fallback
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from html_report_v2 import main  # type: ignore[no-redef]

if __name__ == "__main__":
    raise SystemExit(main())
