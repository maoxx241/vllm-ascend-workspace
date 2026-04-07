#!/usr/bin/env python3
"""Add or attach one managed machine for this workspace.

This is the public agent-facing entrypoint. Prefer this wrapper over calling
``inventory.py`` or ``manage_machine.py`` directly for normal add/attach work.
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
    choose_alias,
    emit_progress,
    find_record,
    host_target,
    image_request_matches_record,
    list_records,
    load_or_create_profile,
    machine_summary,
    print_json,
    probe_host,
    public_key_needs_input_payload,
    resolve_password_args,
    resolve_workflow_image,
    status_payload,
    sync_mesh,
    upsert_machine_record,
    verify_machine,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--host", required=True, help="host IP or DNS name")
    parser.add_argument("--alias", help="optional stable alias; defaults to the host value")
    parser.add_argument("--host-user", default=machine_ops.DEFAULT_HOST_USER, help=f"SSH user (default: {machine_ops.DEFAULT_HOST_USER})")
    parser.add_argument("--host-port", type=int, default=machine_ops.DEFAULT_HOST_PORT, help=f"host SSH port (default: {machine_ops.DEFAULT_HOST_PORT})")
    parser.add_argument("--machine-username", help="workspace machine username; letters and digits only")
    parser.add_argument(
        "--generate-machine-username",
        "--generate",
        dest="generate_machine_username",
        action="store_true",
        default=False,
        help="generate a default workspace machine username; only use after explicit user consent",
    )
    parser.add_argument(
        "--image",
        help=(
            "explicit image selector: `main`, `stable`, or a full non-latest image reference; "
            "this workflow does not default implicitly"
        ),
    )
    parser.add_argument("--workdir", default=machine_ops.DEFAULT_WORKDIR, help=f"container workdir (default: {machine_ops.DEFAULT_WORKDIR})")
    parser.add_argument("--public-key-file", help="local public key to install; defaults to ~/.ssh/id_ed25519.pub if present")
    password_group = parser.add_mutually_exclusive_group()
    password_group.add_argument("--password", help="host password already supplied by the user in the current chat")
    password_group.add_argument("--password-env", help="read the host password from one environment variable")
    password_group.add_argument("--password-stdin", action="store_true", help="read the host password from standard input")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        emit_progress(action="add", phase="profile", message="ensuring local machine profile", machine=args.host)
        profile, needs_profile, profile_action = load_or_create_profile(
            machine_username=args.machine_username,
            generate_machine_username=args.generate_machine_username,
        )
        if needs_profile is not None:
            print_json(needs_profile)
            return 0
        assert profile is not None

        existing = find_record(args.host)
        alias = choose_alias(explicit_alias=args.alias, host=args.host)
        existing_from_host = existing is not None
        if existing is not None:
            alias = existing["alias"]
        else:
            existing = find_record(alias)

        image, image_needs_input = resolve_workflow_image(
            explicit_image=args.image,
            existing_record=existing,
            action="machine add / attach",
        )
        if image_needs_input is not None:
            print_json(image_needs_input)
            return 0
        assert image is not None

        if existing is not None:
            verified = verify_machine(existing)
            if verified.get("status") == "ready" and image_request_matches_record(image, existing):
                message = (
                    "machine is already managed and ready; no mutation was needed"
                    if existing_from_host
                    else "machine alias already exists in inventory and is ready"
                )
                print_json(
                    status_payload(
                        "ready",
                        success=True,
                        action="already-ready",
                        message=message,
                        machine=machine_summary(existing),
                        profile={
                            "action": profile_action,
                            "machine_username": profile["machine_username"],
                            "container_name": profile["container_name"],
                        },
                        verify=verified,
                    )
                )
                return 0
            if verified.get("status") == "blocked":
                print_json(verified)
                return 0

        target_host = existing["host"]["ip"] if existing is not None else args.host
        target_user = existing["host"]["user"] if existing is not None else args.host_user
        target_port = existing["host"]["port"] if existing is not None else args.host_port
        target = host_target(host=target_host, user=target_user, port=target_port)

        private_key = None
        public_key_needs_input = None
        try:
            key_path = machine_ops.find_public_key(args.public_key_file)
            private_key = machine_ops.private_key_for_public_key(key_path)
        except machine_ops.MachineManagementError as exc:
            public_key_needs_input = public_key_needs_input_payload(str(exc))
        if public_key_needs_input is not None:
            print_json(public_key_needs_input)
            return 0

        password = resolve_password_args(args)
        emit_progress(action="add", phase="host-auth", message="checking host key SSH", machine=alias)
        host_ssh_precheck = check_host_key(target, private_key)
        host_auth = None
        if not host_ssh_precheck["ok"]:
            host_auth = bootstrap_host_key(target, public_key_file=args.public_key_file, password=password)
            if host_auth.get("status") == "needs_input":
                print_json(host_auth)
                return 0
            if host_auth.get("status") == "blocked":
                print_json(host_auth)
                return 0
        else:
            host_auth = {"success": True, "result": "already-configured", "precheck": host_ssh_precheck}

        emit_progress(action="add", phase="probe", message="probing host prerequisites", machine=alias)
        probe = probe_host(target, image=image)
        if probe.get("status") == "blocked":
            print_json(probe)
            return 0
        free_port = probe.get("free_port")
        if not isinstance(free_port, int) and existing is None:
            print_json(
                status_payload(
                    "blocked",
                    success=False,
                    action="choose-container-port",
                    message="host probe succeeded but did not produce a free high SSH port",
                    probe=probe,
                )
            )
            return 0

        namespace = existing.get("namespace") if existing is not None else profile["machine_username"]
        container_name = existing["container"]["name"] if existing is not None else profile["container_name"]
        container_port = existing["container"]["ssh_port"] if existing is not None else free_port
        workdir = existing["container"]["workdir"] if existing is not None else args.workdir

        emit_progress(action="add", phase="bootstrap", message="bootstrapping or repairing the managed container", machine=alias)
        container = bootstrap_container(
            target,
            host=target_host,
            container_name=container_name,
            container_ssh_port=container_port,
            image=image,
            workdir=workdir,
            namespace=namespace,
            public_key_file=args.public_key_file,
            replace_container_on_image_change=bool(args.image),
        )
        if container.get("status") in {"needs_input", "needs_repair", "blocked"}:
            print_json(container)
            return 0

        actual_image = container.get("selected_image") or container.get("image") or image
        bootstrap_method = "password-once" if host_ssh_precheck.get("ok") is False and password.value is not None else None
        emit_progress(action="add", phase="inventory", message="persisting machine record and refreshing mesh", machine=alias)
        inventory_payload, record = upsert_machine_record(
            alias=alias,
            namespace=namespace,
            host_ip=target_host,
            host_port=target_port,
            host_user=target_user,
            container_name=container_name,
            container_ssh_port=container_port,
            image=actual_image,
            workdir=workdir,
            bootstrap_method=bootstrap_method,
        )
        peers = [peer for peer in list_records() if peer["alias"] != record["alias"]]
        mesh = sync_mesh(record, peers=peers)
        emit_progress(action="add", phase="verify", message="running final readiness verification", machine=record["alias"])
        verified = verify_machine(
            record,
            progress_cb=lambda phase, message: emit_progress(action="add", phase=phase, message=message, machine=record["alias"]),
        )
        if verified.get("status") == "blocked":
            print_json(verified)
            return 0
        if verified.get("status") != "ready":
            print_json(
                status_payload(
                    "needs_repair",
                    success=False,
                    action="post-bootstrap-verify-failed",
                    message="bootstrap finished but final readiness verification still reports drift",
                    machine=machine_summary(record),
                    profile={
                        "action": profile_action,
                        "machine_username": profile["machine_username"],
                        "container_name": profile["container_name"],
                    },
                    host_auth=host_auth,
                    probe=probe,
                    container=container,
                    inventory=inventory_payload,
                    mesh=mesh,
                    verify=verified,
                )
            )
            return 0

        action = "repaired" if existing is not None else "created"
        print_json(
            status_payload(
                "ready",
                success=True,
                action=action,
                message="machine is managed and ready",
                machine=machine_summary(record),
                profile={
                    "action": profile_action,
                    "machine_username": profile["machine_username"],
                    "container_name": profile["container_name"],
                },
                host_auth=host_auth,
                probe=probe,
                container=container,
                inventory=inventory_payload,
                mesh=mesh,
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
