#!/usr/bin/env python3
"""Repair one managed machine conservatively.

This is the public agent-facing entrypoint. Prefer this wrapper over calling
``manage_machine.py`` or ``inventory.py`` directly for normal repair work.
All outputs are JSON.
"""

from __future__ import annotations

import argparse
from typing import Sequence

import manage_machine as machine_ops
from _workflow_common import (  # noqa: E402
    WorkflowError,
    bootstrap_container,
    bootstrap_host_key,
    check_host_key,
    emit_progress,
    ensure_local_public_key,
    find_record,
    host_target,
    image_request_matches_record,
    list_records,
    machine_summary,
    print_json,
    resolve_password_args,
    resolve_workflow_image,
    status_payload,
    sync_mesh,
    upsert_machine_record,
    verify_machine,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--machine", required=True, help="machine alias or host IP from inventory")
    parser.add_argument(
        "--image",
        help=(
            "explicit replacement image selector: `main`, `stable`, or a full non-latest image reference; "
            "omit only when the recorded image is already explicit and acceptable"
        ),
    )
    parser.add_argument("--public-key-file", help="local public key to install; defaults to ~/.ssh/id_ed25519.pub if present")
    parser.add_argument("--python", help="optional explicit python path inside the container")
    password_group = parser.add_mutually_exclusive_group()
    password_group.add_argument("--password", help="host password already supplied by the user in the current chat")
    password_group.add_argument("--password-env", help="read the host password from one environment variable")
    password_group.add_argument("--password-stdin", action="store_true", help="read the host password from standard input")
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
                    action="repair-skipped",
                    message=f"no managed machine found for {args.machine}",
                )
            )
            return 0

        image, image_needs_input = resolve_workflow_image(
            explicit_image=args.image,
            existing_record=record,
            action="machine repair",
        )
        if image_needs_input is not None:
            print_json(image_needs_input)
            return 0
        assert image is not None

        emit_progress(action="repair", phase="verify-before", message="checking current machine readiness", machine=record["alias"])
        verified_before = verify_machine(
            record,
            python=args.python,
            progress_cb=lambda phase, message: emit_progress(action="repair", phase=f"before-{phase}", message=message, machine=record["alias"]),
        )
        if verified_before.get("status") == "ready" and image_request_matches_record(image, record):
            print_json(
                status_payload(
                    "ready",
                    success=True,
                    action="already-ready",
                    message="machine is already ready; no repair was needed",
                    machine=machine_summary(record),
                    verify=verified_before,
                )
            )
            return 0
        if verified_before.get("status") == "blocked":
            print_json(verified_before)
            return 0

        _, _, _, public_key_needs_input = ensure_local_public_key(args.public_key_file)
        if public_key_needs_input is not None:
            print_json(public_key_needs_input)
            return 0

        password = resolve_password_args(args)
        emit_progress(action="repair", phase="host-auth", message="checking host key SSH", machine=record["alias"])
        target = host_target(
            host=record["host"]["ip"],
            user=record["host"]["user"],
            port=record["host"]["port"],
        )
        try:
            private_key = machine_ops.private_key_for_public_key(machine_ops.find_public_key(args.public_key_file))
        except machine_ops.MachineManagementError:
            private_key = None
        host_ssh_precheck = check_host_key(target, private_key)
        host_auth = {"success": True, "result": "already-configured", "precheck": host_ssh_precheck}
        if not host_ssh_precheck["ok"]:
            host_auth = bootstrap_host_key(target, public_key_file=args.public_key_file, password=password)
            if host_auth.get("status") in {"needs_input", "blocked"}:
                print_json(host_auth)
                return 0

        emit_progress(action="repair", phase="bootstrap", message="repairing the managed container", machine=record["alias"])
        container = bootstrap_container(
            target,
            host=record["host"]["ip"],
            container_name=record["container"]["name"],
            container_ssh_port=record["container"]["ssh_port"],
            image=image,
            workdir=record["container"]["workdir"],
            namespace=record.get("namespace"),
            public_key_file=args.public_key_file,
            replace_container_on_image_change=bool(args.image),
        )
        if container.get("status") in {"needs_input", "needs_repair", "blocked"}:
            print_json(container)
            return 0

        actual_image = container.get("selected_image") or container.get("image") or image
        bootstrap_method = "password-once" if host_ssh_precheck.get("ok") is False and password.value is not None else None
        emit_progress(action="repair", phase="inventory", message="refreshing inventory and mesh state", machine=record["alias"])
        inventory_payload, updated_record = upsert_machine_record(
            alias=record["alias"],
            namespace=record.get("namespace"),
            host_ip=record["host"]["ip"],
            host_port=record["host"]["port"],
            host_user=record["host"]["user"],
            container_name=record["container"]["name"],
            container_ssh_port=record["container"]["ssh_port"],
            image=actual_image,
            workdir=record["container"]["workdir"],
            bootstrap_method=bootstrap_method,
        )
        peers = [peer for peer in list_records() if peer["alias"] != updated_record["alias"]]
        mesh = sync_mesh(updated_record, peers=peers)
        emit_progress(action="repair", phase="verify-after", message="running post-repair readiness verification", machine=updated_record["alias"])
        verified_after = verify_machine(
            updated_record,
            python=args.python,
            progress_cb=lambda phase, message: emit_progress(action="repair", phase=f"after-{phase}", message=message, machine=updated_record["alias"]),
        )
        if verified_after.get("status") == "blocked":
            print_json(verified_after)
            return 0
        if verified_after.get("status") != "ready":
            print_json(
                status_payload(
                    "needs_repair",
                    success=False,
                    action="repair-incomplete",
                    message="repair ran but the machine is still not ready",
                    machine=machine_summary(updated_record),
                    verify_before=verified_before,
                    host_auth=host_auth,
                    container=container,
                    inventory=inventory_payload,
                    mesh=mesh,
                    verify=verified_after,
                )
            )
            return 0

        print_json(
            status_payload(
                "ready",
                success=True,
                action="repaired",
                message="machine repair completed successfully",
                machine=machine_summary(updated_record),
                verify_before=verified_before,
                host_auth=host_auth,
                container=container,
                inventory=inventory_payload,
                mesh=mesh,
                verify=verified_after,
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
