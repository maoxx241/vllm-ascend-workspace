#!/usr/bin/env python3
"""Remove one managed machine from this workspace.

This is the public agent-facing entrypoint. Prefer this wrapper over calling
``manage_machine.py remove-container`` or ``inventory.py remove`` directly for
normal removal work. All outputs are JSON.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from _workflow_common import (  # noqa: E402
    WorkflowError,
    cleanup_mesh,
    find_record,
    list_records,
    machine_summary,
    print_json,
    remove_container,
    remove_machine_record,
    status_payload,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--machine", required=True, help="machine alias or host IP from inventory")
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
                    action="remove-skipped",
                    message=f"no managed machine found for {args.machine}",
                )
            )
            return 0

        peers = [peer for peer in list_records() if peer["alias"] != record["alias"]]
        mesh_cleanup = cleanup_mesh(record, peers=peers)
        removed_container = remove_container(record)
        if removed_container.get("status") == "blocked":
            print_json(removed_container)
            return 0

        inventory_payload, _ = remove_machine_record(record["alias"])
        print_json(
            status_payload(
                "removed",
                success=True,
                action="removed",
                message="managed machine was removed from the workspace inventory and container layer",
                machine=machine_summary(record),
                mesh=mesh_cleanup,
                container=removed_container,
                inventory=inventory_payload,
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
