#!/usr/bin/env python3
"""Verify one managed machine read-only.

This is the public agent-facing entrypoint. Prefer this wrapper over calling
``manage_machine.py verify-machine`` directly for normal readiness checks.
All outputs are JSON.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from _workflow_common import (  # noqa: E402
    WorkflowError,
    find_record,
    machine_summary,
    print_json,
    status_payload,
    verify_machine,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--machine", required=True, help="machine alias or host IP from inventory")
    parser.add_argument("--python", help="optional explicit python path inside the container")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        record = find_record(args.machine)
        if record is None:
            print_json(
                status_payload(
                    "unmanaged",
                    success=False,
                    action="verify-skipped",
                    message=f"no managed machine found for {args.machine}",
                )
            )
            return 0
        verified = verify_machine(record, python=args.python)
        if verified.get("status") == "ready":
            print_json(verified)
            return 0
        if verified.get("status") == "blocked":
            print_json(verified)
            return 0
        print_json(
            status_payload(
                "needs_repair",
                success=False,
                action="verify-found-drift",
                message="machine is managed but not ready",
                machine=machine_summary(record),
                verify=verified,
            )
        )
        return 0
    except WorkflowError as exc:
        print_json({"success": False, "status": "blocked", "action": "failed", "error": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001
        print_json({"success": False, "status": "blocked", "action": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
