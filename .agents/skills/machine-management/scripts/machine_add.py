#!/usr/bin/env python3
"""Record the project username used for vaws-<user> containers.

Container bootstrap and recipe execution belong to vaws-coordinator.
This entry only stores the local machine username document.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_local_state import default_container_name, ensure_profile, profile_summary  # noqa: E402
from vaws_result_envelope import emit_skill_json  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--machine-username", help="letters and digits; becomes vaws-<user>")
    parser.add_argument("--generate-machine-username", action="store_true")
    args = parser.parse_args(argv)
    try:
        profile, action = ensure_profile(
            machine_username=args.machine_username,
            allow_update=False,
            generate=args.generate_machine_username,
        )
        payload = {
            "status": "ok",
            "action": action,
            "machine_username": profile["machine_username"],
            "container_name": default_container_name(profile["machine_username"]),
            "profile": profile_summary(),
            "next_steps": [
                "Coordinator owns container bootstrap and runtime registration.",
                "Call python -m vaws_coordinator provision --host <host> --image <image> --user <user>.",
                "Do not create per-task Docker names from this workspace.",
            ],
        }
        emit_skill_json(payload, skill="machine-management", entry_point=".agents/skills/machine-management/scripts/machine_add.py")
        return 0
    except Exception as exc:
        emit_skill_json({"status": "failed", "error": str(exc)}, skill="machine-management", entry_point=".agents/skills/machine-management/scripts/machine_add.py")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
