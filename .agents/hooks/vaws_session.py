#!/usr/bin/env python3
"""Launch the coordinator's native session hook with workspace dependencies.

The hook that writes the local task registry lives in the vaws-coordinator
package. This entry selects the workspace interpreter and passes the native
client event through; it never writes the registry itself.

Generated setup commands may pass ``--agent-sessions-dir`` so a GUI client
does not need the setup shell's environment.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))
from vaws_venv import ensure_workspace_interpreter  # noqa: E402
from vaws_environment import PIN_ENV  # noqa: E402

_bootstrap = argparse.ArgumentParser(add_help=False)
_bootstrap.add_argument("--environment-receipt")
_pin, _ = _bootstrap.parse_known_args()
if _pin.environment_receipt:
    os.environ[PIN_ENV] = _pin.environment_receipt

# The native client has already selected its directory and environment.
# Updates belong before creation of a new editing copy, never in task hooks.
ensure_workspace_interpreter(repo_root=ROOT)

from vaws_coordinator_launch import CoordinatorUnavailable, exec_module  # noqa: E402
from vaws_dependency import REMEDY  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--agent-sessions-dir", default="")
    parser.add_argument("--environment-receipt", help=argparse.SUPPRESS)
    parser.add_argument("--github-identity-file", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.agent_sessions_dir.strip():
        os.environ["VAWS_AGENT_SESSIONS_DIR"] = str(Path(args.agent_sessions_dir).expanduser())
    if args.github_identity_file:
        os.environ["VAWS_GITHUB_IDENTITY_FILE"] = str(Path(args.github_identity_file).expanduser())
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
