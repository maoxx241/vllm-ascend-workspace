#!/usr/bin/env python3
"""Shared workflow helpers for machine-management task entrypoints.

These wrappers intentionally expose a much narrower public interface than the
low-level maintenance helpers. The task scripts in this directory are the
agent-facing surface; ``inventory.py`` and ``manage_machine.py`` remain
available as implementation helpers.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Callable, Literal, Sequence

ROOT = pathlib.Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import inventory as inventory_store  # noqa: E402
import manage_machine as machine_ops  # noqa: E402
from vaws_local_state import (  # noqa: E402
    WorkspaceStateError,
    ensure_profile,
    load_profile,
    profile_summary,
    utc_now_iso,
)

Status = Literal[
    "ready",
    "removed",
    "needs_input",
    "needs_repair",
    "blocked",
    "unmanaged",
]


class WorkflowError(RuntimeError):
    """Raised for deterministic workflow-layer failures."""


@dataclass(frozen=True)
class PasswordInput:
    value: str | None
    source: str | None
    env_name: str


@dataclass(frozen=True)
class InventoryState:
    requested_path: pathlib.Path
    active_path: pathlib.Path
    inventory: dict[str, Any]


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def emit_progress(*, action: str, phase: str, message: str, machine: str | None = None, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "action": action,
        "phase": phase,
        "message": message,
    }
    if machine is not None:
        payload["machine"] = machine
    payload.update({key: value for key, value in extra.items() if value is not None})
    sys.stderr.write(machine_ops.PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def status_payload(
    status: Status,
    *,
    success: bool,
    action: str,
    message: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": success,
        "status": status,
        "action": action,
    }
    if message is not None:
        payload["message"] = message
    payload.update(extra)
    return payload


def profile_needs_input_payload() -> dict[str, Any]:
    summary = profile_summary()
    return status_payload(
        "needs_input",
        success=False,
        action="await-machine-username",
        message="local machine profile is missing; ask once for a machine username or explicitly accept a generated default",
        missing={
            "name": "machine_username",
            "rules": summary["username_rules"],
            "default_random_allowed": True,
            "default_random_requires_explicit_user_consent": True,
        },
        profile=summary,
    )


def public_key_needs_input_payload(error: str) -> dict[str, Any]:
    return status_payload(
        "needs_input",
        success=False,
        action="await-local-public-key",
        message=error,
        missing={
            "name": "local_public_key",
            "expected": "~/.ssh/id_ed25519.pub or another SSH public key file",
        },
    )


def host_password_needs_input_payload(target: machine_ops.SshTarget) -> dict[str, Any]:
    return status_payload(
        "needs_input",
        success=False,
        action="await-host-password",
        message="host key SSH is not configured yet; rerun with one approved password source for the initial bootstrap",
        missing={
            "name": "host_password",
            "accepted_flags": ["--password", "--password-env", "--password-stdin"],
            "target": {
                "host": target.host,
                "user": target.user,
                "host_port": target.port,
            },
        },
    )


def load_inventory_state() -> InventoryState:
    requested_path = inventory_store.preferred_inventory_path(inventory_store.DEFAULT_PATH)
    active_path = inventory_store.read_inventory_path(requested_path)
    inventory = inventory_store.load_inventory(active_path)
    return InventoryState(requested_path=requested_path, active_path=active_path, inventory=inventory)


def find_record(identifier: str, state: InventoryState | None = None) -> dict[str, Any] | None:
    current = state or load_inventory_state()
    matches = inventory_store._find_matches(current.inventory, identifier=identifier)  # noqa: SLF001
    if not matches:
        return None
    if len(matches) > 1:
        raise WorkflowError(
            f"multiple machines matched {identifier!r}; use a unique alias or host IP"
        )
    return matches[0]


def list_records(state: InventoryState | None = None) -> list[dict[str, Any]]:
    current = state or load_inventory_state()
    return list(current.inventory["machines"])


def machine_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "alias": record["alias"],
        "namespace": record.get("namespace"),
        "bootstrap_method": record.get("bootstrap_method"),
        "host": {
            "ip": record["host"]["ip"],
            "user": record["host"]["user"],
            "port": record["host"]["port"],
        },
        "container": {
            "name": record["container"]["name"],
            "ssh_port": record["container"]["ssh_port"],
            "image": record["container"]["image"],
            "workdir": record["container"]["workdir"],
        },
        "last_verified_at": record.get("last_verified_at"),
    }


def load_or_create_profile(
    *,
    machine_username: str | None,
    generate_machine_username: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    summary = profile_summary()
    if summary["exists"]:
        if machine_username is not None:
            try:
                profile, action = ensure_profile(
                    machine_username=machine_username,
                    allow_update=True,
                    generate=False,
                )
            except WorkspaceStateError as exc:
                raise WorkflowError(str(exc)) from exc
            return profile, None, action
        profile = load_profile()
        if profile is None:
            raise WorkflowError("profile summary reported exists=true but the profile could not be loaded")
        return profile, None, "existing"

    if machine_username is None and not generate_machine_username:
        return None, profile_needs_input_payload(), None

    try:
        profile, action = ensure_profile(
            machine_username=machine_username,
            allow_update=False,
            generate=generate_machine_username,
        )
    except WorkspaceStateError as exc:
        raise WorkflowError(str(exc)) from exc
    return profile, None, action


def ensure_local_public_key(
    public_key_file: str | None = None,
) -> tuple[pathlib.Path | None, pathlib.Path | None, str | None, dict[str, Any] | None]:
    try:
        key_path = machine_ops.find_public_key(public_key_file)
        private_key = machine_ops.private_key_for_public_key(key_path)
        public_key = machine_ops.load_public_key(key_path)
    except machine_ops.MachineManagementError as exc:
        return None, None, None, public_key_needs_input_payload(str(exc))
    return key_path, private_key, public_key, None


def resolve_password_args(args: argparse.Namespace) -> PasswordInput:
    if getattr(args, "password", None) is not None:
        return PasswordInput(value=args.password, source="password", env_name=machine_ops.DEFAULT_PASSWORD_ENV)
    if getattr(args, "password_env", None):
        env_name = machine_ops.validate_env_name(args.password_env)
        value = os.environ.get(env_name)
        if value is None:
            raise WorkflowError(
                f"environment variable {env_name} is not set; export it before calling the workflow"
            )
        return PasswordInput(value=value, source="password-env", env_name=env_name)
    if getattr(args, "password_stdin", False):
        value = sys.stdin.read()
        value = value.rstrip("\r\n")
        if not value:
            raise WorkflowError("no password received on stdin")
        return PasswordInput(value=value, source="password-stdin", env_name=machine_ops.DEFAULT_PASSWORD_ENV)
    return PasswordInput(value=None, source=None, env_name=machine_ops.DEFAULT_PASSWORD_ENV)


def host_target(*, host: str, user: str, port: int) -> machine_ops.SshTarget:
    return machine_ops.SshTarget(host=host, user=user, port=port)


def container_target(record: dict[str, Any]) -> machine_ops.SshTarget:
    return machine_ops.SshTarget(
        host=record["host"]["ip"],
        user="root",
        port=record["container"]["ssh_port"],
    )


def check_host_key(target: machine_ops.SshTarget, private_key: pathlib.Path | None) -> dict[str, Any]:
    return machine_ops.check_direct_ssh(target, identity_file=private_key)


def bootstrap_host_key(
    target: machine_ops.SshTarget,
    *,
    public_key_file: str | None,
    password: PasswordInput,
) -> dict[str, Any]:
    key_path, private_key, public_key, needs_input = ensure_local_public_key(public_key_file)
    if needs_input is not None:
        return needs_input
    assert key_path is not None and public_key is not None
    before = check_host_key(target, private_key)
    payload: dict[str, Any] = {
        "target": {"host": target.host, "user": target.user, "host_port": target.port},
        "public_key_file": str(key_path),
        "private_key_file": str(private_key) if private_key is not None else None,
        "precheck": before,
        "password_mode": password.source,
        "needs_interactive_terminal": False,
    }
    if before["ok"]:
        payload.update({"success": True, "result": "already-configured", "executed": False})
        return payload
    if password.value is None:
        return host_password_needs_input_payload(target)

    _, command = machine_ops.build_bootstrap_host_key_command(
        target,
        key_path=key_path,
        public_key=public_key,
    )
    proc = machine_ops.run_with_askpass(command, password=password.value, env_name=password.env_name)
    after = check_host_key(target, private_key)
    payload.update(
        {
            "executed": True,
            "command_returncode": proc.returncode,
            "postcheck": after,
            "success": after["ok"],
            "result": "bootstrapped" if after["ok"] else "failed",
            "stdout_tail": machine_ops.compact_failure_tail(proc.stdout),
            "stderr_tail": machine_ops.compact_failure_tail(proc.stderr),
        }
    )
    if not after["ok"]:
        return status_payload(
            "blocked",
            success=False,
            action="host-key-bootstrap-failed",
            message="password-based host bootstrap did not establish key-based SSH",
            target=payload["target"],
            details=payload,
        )
    return payload


def probe_host(
    target: machine_ops.SshTarget,
    *,
    image: str = machine_ops.DEFAULT_IMAGE,
    port_range: str = machine_ops.DEFAULT_PORT_RANGE,
    managed_prefix: str = "vaws-",
) -> dict[str, Any]:
    result = machine_ops.run_remote_script(
        target,
        machine_ops.render_host_probe_script(),
        args=[image, port_range, managed_prefix],
        batch_mode=True,
        timeout_seconds=machine_ops.DEFAULT_PROBE_TIMEOUT_SECONDS,
    )
    try:
        payload = machine_ops.assert_remote_success(result)
    except machine_ops.MachineManagementError as exc:
        return status_payload(
            "blocked",
            success=False,
            action="probe-host",
            message=str(exc),
            target={"host": target.host, "user": target.user, "host_port": target.port},
            stderr_tail=machine_ops.compact_failure_tail(result.stderr),
        )
    payload["target"] = {"host": target.host, "user": target.user, "host_port": target.port}
    if result.progress_events:
        payload["progress_events"] = result.progress_events
    return payload


def bootstrap_container(
    target: machine_ops.SshTarget,
    *,
    host: str,
    container_name: str,
    container_ssh_port: int,
    image: str,
    workdir: str,
    namespace: str | None,
    public_key_file: str | None = None,
) -> dict[str, Any]:
    key_path, private_key, public_key, needs_input = ensure_local_public_key(public_key_file)
    if needs_input is not None:
        return needs_input
    assert key_path is not None and public_key is not None

    result = machine_ops.run_remote_script(
        target,
        machine_ops.render_bootstrap_host_script(),
        args=[container_name, str(container_ssh_port), image, workdir, public_key, namespace or ""],
        batch_mode=True,
        timeout_seconds=machine_ops.DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
    )
    try:
        payload = machine_ops.assert_remote_success(result)
    except machine_ops.MachineManagementError as exc:
        return status_payload(
            "blocked",
            success=False,
            action="bootstrap-container",
            message=str(exc),
            target={
                "host": target.host,
                "user": target.user,
                "host_port": target.port,
                "container_ssh_port": container_ssh_port,
            },
            stderr_tail=machine_ops.compact_failure_tail(result.stderr),
        )

    ssh_check = machine_ops.check_direct_ssh(
        machine_ops.SshTarget(host=host, user="root", port=container_ssh_port),
        identity_file=private_key,
    )
    payload.update(
        {
            "public_key_file": str(key_path),
            "private_key_file": str(private_key) if private_key is not None else None,
            "namespace": namespace,
            "direct_container_ssh": ssh_check,
            "target": {
                "host": target.host,
                "user": target.user,
                "host_port": target.port,
                "container_ssh_port": container_ssh_port,
            },
        }
    )
    if result.progress_events:
        payload["progress_events"] = result.progress_events
    if not ssh_check["ok"]:
        return status_payload(
            "needs_repair",
            success=False,
            action="container-ssh-still-down",
            message="container SSH did not come up after bootstrap",
            details=payload,
        )
    return payload


def smoke_machine(record: dict[str, Any], *, python: str | None = None) -> dict[str, Any]:
    args = argparse.Namespace(
        host=record["host"]["ip"],
        user=record["host"]["user"],
        container_ssh_port=record["container"]["ssh_port"],
        python=python,
    )
    try:
        return machine_ops.smoke_payload(args)
    except machine_ops.MachineManagementError as exc:
        return status_payload(
            "needs_repair",
            success=False,
            action="smoke-failed",
            message=str(exc),
            machine=machine_summary(record),
        )


def verify_machine(
    record: dict[str, Any],
    *,
    python: str | None = None,
    progress_cb: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    try:
        identity_file = machine_ops.private_key_for_public_key(machine_ops.find_public_key(None))
    except machine_ops.MachineManagementError:
        identity_file = None

    if progress_cb is not None:
        progress_cb("host-ssh", "checking host SSH")

    host = host_target(
        host=record["host"]["ip"],
        user=record["host"]["user"],
        port=record["host"]["port"],
    )
    container = container_target(record)
    host_check = machine_ops.check_direct_ssh(host, identity_file=identity_file)
    if progress_cb is not None:
        progress_cb("container-ssh", "checking direct container SSH")
    container_check = machine_ops.check_direct_ssh(container, identity_file=identity_file)
    payload: dict[str, Any] = {
        "machine": machine_summary(record),
        "identity_file": str(identity_file) if identity_file is not None else None,
        "host_ssh": host_check,
        "container_ssh": container_check,
    }
    local_tool_errors = []
    for check in (host_check, container_check):
        stderr = check.get("stderr")
        if isinstance(stderr, str) and stderr.startswith("required local command not found:"):
            local_tool_errors.append(stderr)
    if local_tool_errors:
        payload.update(
            {
                "success": False,
                "status": "blocked",
                "action": "missing-local-tool",
                "message": "required local SSH tooling is missing",
                "local_tool_errors": sorted(set(local_tool_errors)),
                "ready": False,
                "smoke": {"success": False, "skipped": "local ssh command is unavailable"},
            }
        )
        return payload
    if container_check["ok"]:
        if progress_cb is not None:
            progress_cb("smoke", "running torch/torch_npu smoke")
        smoke = smoke_machine(record, python=python)
    else:
        smoke = {"success": False, "skipped": "container SSH failed"}
    payload["smoke"] = smoke
    payload["ready"] = bool(host_check["ok"] and container_check["ok"] and smoke.get("success") is True)
    if progress_cb is not None:
        progress_cb("complete", "machine verification finished")
    if payload["ready"]:
        payload.update({"success": True, "status": "ready", "action": "verified"})
        return payload
    payload.update(
        {
            "success": False,
            "status": "needs_repair",
            "action": "verify-found-drift",
            "message": "machine is managed but not ready",
        }
    )
    return payload


def mesh_comment_for_record(record: dict[str, Any]) -> str:
    return f"vaws-mesh:{record['host']['ip']}"


def export_mesh_key(record: dict[str, Any]) -> dict[str, Any]:
    target = container_target(record)
    result = machine_ops.run_remote_script(
        target,
        machine_ops.render_mesh_export_key_script(),
        args=[mesh_comment_for_record(record)],
        batch_mode=True,
    )
    return machine_ops.assert_remote_success(result)


def add_mesh_peer(record: dict[str, Any], *, peer_public_key: str, peer_host: str, peer_port: int) -> dict[str, Any]:
    target = container_target(record)
    result = machine_ops.run_remote_script(
        target,
        machine_ops.render_mesh_add_peer_script(),
        args=[peer_public_key, peer_host, str(peer_port)],
        batch_mode=True,
    )
    return machine_ops.assert_remote_success(result)


def remove_mesh_peer(record: dict[str, Any], *, peer_comment: str, peer_host: str, peer_port: int) -> dict[str, Any]:
    target = container_target(record)
    result = machine_ops.run_remote_script(
        target,
        machine_ops.render_mesh_remove_peer_script(),
        args=[peer_comment, peer_host, str(peer_port)],
        batch_mode=True,
    )
    return machine_ops.assert_remote_success(result)


def sync_mesh(record: dict[str, Any], *, peers: Sequence[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if not peers:
        return {"attempted": 0, "success": True, "peers": results}

    try:
        own_key = export_mesh_key(record)
    except machine_ops.MachineManagementError as exc:
        return {"attempted": 0, "success": False, "error": str(exc), "peers": results}

    overall_success = True
    for peer in peers:
        peer_summary = machine_summary(peer)
        entry: dict[str, Any] = {"peer": peer_summary, "success": True}
        try:
            peer_key = export_mesh_key(peer)
            entry["add_peer_to_new"] = add_mesh_peer(
                record,
                peer_public_key=peer_key["public_key"],
                peer_host=peer["host"]["ip"],
                peer_port=peer["container"]["ssh_port"],
            )
            entry["add_new_to_peer"] = add_mesh_peer(
                peer,
                peer_public_key=own_key["public_key"],
                peer_host=record["host"]["ip"],
                peer_port=record["container"]["ssh_port"],
            )
        except Exception as exc:  # noqa: BLE001
            entry["success"] = False
            entry["error"] = str(exc)
            overall_success = False
        results.append(entry)
    return {"attempted": len(peers), "success": overall_success, "peers": results}


def cleanup_mesh(record: dict[str, Any], *, peers: Sequence[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    comment = mesh_comment_for_record(record)
    overall_success = True
    for peer in peers:
        entry: dict[str, Any] = {"peer": machine_summary(peer), "success": True}
        try:
            entry["remove_from_peer"] = remove_mesh_peer(
                peer,
                peer_comment=comment,
                peer_host=record["host"]["ip"],
                peer_port=record["container"]["ssh_port"],
            )
        except Exception as exc:  # noqa: BLE001
            entry["success"] = False
            entry["error"] = str(exc)
            overall_success = False
        results.append(entry)
    return {"attempted": len(peers), "success": overall_success, "peers": results}


def choose_alias(*, explicit_alias: str | None, host: str) -> str:
    value = (explicit_alias or host).strip()
    if not value:
        raise WorkflowError("machine alias must be non-empty")
    return value


def upsert_machine_record(
    *,
    alias: str,
    namespace: str | None,
    host_ip: str,
    host_port: int,
    host_user: str,
    container_name: str,
    container_ssh_port: int,
    image: str,
    workdir: str,
    bootstrap_method: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_inventory_state()
    alias_matches = inventory_store._find_matches(state.inventory, alias=alias)  # noqa: SLF001
    ip_matches = inventory_store._find_matches(state.inventory, host_ip=host_ip)  # noqa: SLF001
    alias_record = alias_matches[0] if alias_matches else None
    ip_record = ip_matches[0] if ip_matches else None
    if alias_record is not None and ip_record is not None and alias_record is not ip_record:
        raise WorkflowError(
            "alias and host IP match different existing records; resolve the conflict manually"
        )
    target = alias_record or ip_record
    normalized_namespace = inventory_store.validate_machine_username(namespace) if namespace else None
    record = {
        "alias": alias,
        "namespace": normalized_namespace,
        "host": {
            "ip": host_ip,
            "port": host_port,
            "user": host_user,
        },
        "container": {
            "name": container_name,
            "ssh_port": container_ssh_port,
            "image": image,
            "workdir": workdir,
        },
        "bootstrap_method": inventory_store.resolve_bootstrap_method(
            bootstrap_method,
            existing_record=target,
        ),
        "managed_by_skill": True,
        "created_by_skill": True,
        "last_verified_at": utc_now_iso(),
    }
    if normalized_namespace is None:
        record.pop("namespace")
    inventory_store._validate_record(record)  # noqa: SLF001

    if target is None:
        state.inventory["machines"].append(record)
        action = "inserted"
    else:
        target.clear()
        target.update(record)
        action = "updated"

    inventory_store.save_inventory(state.requested_path, state.inventory)
    payload: dict[str, Any] = {
        "result": action,
        "alias": record["alias"],
        "namespace": record.get("namespace"),
        "host_ip": record["host"]["ip"],
        "inventory": str(state.requested_path),
    }
    if not inventory_store.same_path(state.active_path, state.requested_path):
        payload["loaded_from"] = str(state.active_path)
        payload["migrated_from_legacy"] = inventory_store.same_path(
            state.active_path,
            inventory_store.LEGACY_INVENTORY_PATH,
        )
    return payload, record


def remove_machine_record(identifier: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state = load_inventory_state()
    matches = inventory_store._find_matches(state.inventory, identifier=identifier)  # noqa: SLF001
    if not matches:
        return status_payload(
            "unmanaged",
            success=False,
            action="remove-skipped",
            message=f"no managed machine found for {identifier}",
        ), None
    if len(matches) > 1:
        raise WorkflowError(
            f"multiple machines matched {identifier!r}; use a unique alias or host IP"
        )
    target = matches[0]
    state.inventory["machines"] = [record for record in state.inventory["machines"] if record is not target]
    inventory_store.save_inventory(state.requested_path, state.inventory)
    payload: dict[str, Any] = {
        "result": "removed",
        "alias": target["alias"],
        "namespace": target.get("namespace"),
        "host_ip": target["host"]["ip"],
        "inventory": str(state.requested_path),
    }
    if not inventory_store.same_path(state.active_path, state.requested_path):
        payload["loaded_from"] = str(state.active_path)
        payload["migrated_from_legacy"] = inventory_store.same_path(
            state.active_path,
            inventory_store.LEGACY_INVENTORY_PATH,
        )
    return payload, target


def remove_container(record: dict[str, Any]) -> dict[str, Any]:
    target = host_target(
        host=record["host"]["ip"],
        user=record["host"]["user"],
        port=record["host"]["port"],
    )
    result = machine_ops.run_remote_script(
        target,
        machine_ops.render_remove_container_host_script(),
        args=[record["container"]["name"]],
        batch_mode=True,
    )
    try:
        payload = machine_ops.assert_remote_success(result)
    except machine_ops.MachineManagementError as exc:
        return status_payload(
            "blocked",
            success=False,
            action="remove-container",
            message=str(exc),
            machine=machine_summary(record),
            stderr_tail=machine_ops.compact_failure_tail(result.stderr),
        )
    payload["local_known_hosts_cleanup"] = machine_ops.remove_known_host_entry(
        record["host"]["ip"],
        record["container"]["ssh_port"],
        machine_ops.DEFAULT_KNOWN_HOSTS.expanduser().resolve(),
    )
    payload["target"] = {
        "host": target.host,
        "user": target.user,
        "host_port": target.port,
    }
    return payload
