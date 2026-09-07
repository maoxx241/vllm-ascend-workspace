#!/usr/bin/env python3
"""Compatibility adapter: exec the coordinator's native session hook.

The hook that writes the local task registry lives in the vaws-coordinator
checkout. This file remains at the historical path so already-installed client
hook commands keep working. It never writes the registry itself.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))
from vaws_coordinator import CoordinatorUnavailable, coordinator_environment, coordinator_root


def main() -> int:
    try:
        checkout = coordinator_root()
    except CoordinatorUnavailable as exc:
        sys.stdin.read()
        print(
            f"VAWS local association unavailable: {exc}. Local tools remain usable.",
            file=sys.stderr,
        )
        print("")
        return 0
    script = checkout / "hooks" / "vaws_session.py"
    if not script.is_file():
        print(
            f"VAWS local association unavailable: {script} is missing. Local tools remain usable.",
            file=sys.stderr,
        )
        print("")
        return 0
    os.execve(sys.executable, [sys.executable, str(script), *sys.argv[1:]], coordinator_environment())
    return 0  # pragma: no cover - execve does not return


if __name__ == "__main__":
    raise SystemExit(main())
