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
    import sys
    from pathlib import Path
    _repo_root = Path(__file__).resolve().parents[6]
    _lib = _repo_root / ".agents" / "lib"
    if str(_lib) not in sys.path:
        sys.path.insert(0, str(_lib))
    from vaws_venv import ensure_workspace_interpreter
    ensure_workspace_interpreter(repo_root=_repo_root)
    raise SystemExit(main())
