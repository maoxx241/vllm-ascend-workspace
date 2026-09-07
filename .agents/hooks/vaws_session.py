#!/usr/bin/env python3
"""Compatibility adapter: exec the coordinator's native session hook.

The hook that writes the local task registry lives in the vaws-coordinator
checkout. This file remains at the historical path so already-installed client
hook commands keep working. It never writes the registry itself.

Generated setup commands may pass ``--coordinator-root`` and
``--agent-sessions-dir`` so a GUI client does not need the setup shell's
environment. Those flags are stripped before exec'ing the coordinator hook.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))
from vaws_coordinator import COORDINATOR_ROOT_ENV, CoordinatorUnavailable, coordinator_environment, coordinator_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--coordinator-root", default="")
    parser.add_argument("--agent-sessions-dir", default="")
    args = parser.parse_args()
    if args.coordinator_root.strip():
        os.environ[COORDINATOR_ROOT_ENV] = str(Path(args.coordinator_root).expanduser())
    if args.agent_sessions_dir.strip():
        os.environ["VAWS_AGENT_SESSIONS_DIR"] = str(Path(args.agent_sessions_dir).expanduser())
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
    forwarded = ["--client", args.client]
    if args.project is not None:
        forwarded += ["--project", str(args.project)]
    os.execve(sys.executable, [sys.executable, str(script), *forwarded], coordinator_environment())
    return 0  # pragma: no cover - execve does not return


if __name__ == "__main__":
    raise SystemExit(main())
