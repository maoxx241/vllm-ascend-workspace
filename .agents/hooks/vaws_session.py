#!/usr/bin/env python3
"""Compatibility adapter: exec the coordinator's native session hook.

The hook that writes the local task registry lives in the vaws-coordinator
package. This file remains at the historical path so already-installed client
hook commands keep working. It never writes the registry itself.

Generated setup commands may pass ``--agent-sessions-dir`` so a GUI client
does not need the setup shell's environment. ``--coordinator-root`` is
accepted and ignored (the package does not read that variable).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))
from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_coordinator_launch import CoordinatorUnavailable, exec_module  # noqa: E402
from vaws_dependency import REMEDY  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--coordinator-root", default="")
    parser.add_argument("--agent-sessions-dir", default="")
    args = parser.parse_args()
    if args.agent_sessions_dir.strip():
        os.environ["VAWS_AGENT_SESSIONS_DIR"] = str(Path(args.agent_sessions_dir).expanduser())
    forwarded = ["--client", args.client]
    if args.project is not None:
        forwarded += ["--project", str(args.project)]
    try:
        return exec_module("vaws_coordinator.hooks.vaws_session", forwarded)
    except CoordinatorUnavailable as exc:
        sys.stdin.read()
        print(
            f"VAWS local association unavailable: {exc}. Run `{REMEDY}`. Local tools remain usable.",
            file=sys.stderr,
        )
        print("")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
