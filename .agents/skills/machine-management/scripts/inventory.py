#!/usr/bin/env python3
"""Low-level local machine inventory helper for vllm-ascend-workspace.

The canonical inventory now lives under `.vaws-local/machine-inventory.json`.
For compatibility, the helper will read the legacy repo-root
`.machine-inventory.json` when the new path does not exist yet. Prefer the
task-oriented wrappers for normal add / verify / repair / remove workflows.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

LIB_DIR = Path(__file__).resolve().parents[3] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import (  # noqa: E402
    INVENTORY_PATH as DEFAULT_PATH,
    LEGACY_INVENTORY_PATH,
    ensure_state_dir,
    resolve_inventory_read_path,
    same_path,
    validate_machine_username,
)

SCHEMA_VERSION = 1
STATE_LOCK_SUFFIX = ".lock"
DEFAULT_LOCK_TIMEOUT_SECONDS = 15.0
DEFAULT_LOCK_POLL_SECONDS = 0.05


class InventoryError(RuntimeError):
    pass


BOOTSTRAP_METHOD_ALIASES = {
    "ssh": "ssh",
    "key": "ssh",
    "password-once": "password-once",
    "password_once": "password-once",
    "password": "password-once",
}
DEFAULT_BOOTSTRAP_METHOD = "ssh"
INPUT_BOOTSTRAP_METHOD_CHOICES = ["auto", *sorted(BOOTSTRAP_METHOD_ALIASES)]


def normalize_bootstrap_method(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = BOOTSTRAP_METHOD_ALIASES.get(value)
    if normalized is None:
        choices = ", ".join(sorted(BOOTSTRAP_METHOD_ALIASES))
        raise InventoryError(f"unsupported bootstrap_method {value!r}; expected one of: {choices}")
    return normalized


def resolve_bootstrap_method(value: str | None, existing_record: dict[str, Any] | None = None) -> str:
    if value in {None, "", "auto"}:
        existing = normalize_bootstrap_method(existing_record.get("bootstrap_method")) if existing_record else None
        return existing or DEFAULT_BOOTSTRAP_METHOD
    normalized = normalize_bootstrap_method(value)
    if normalized is None:
        raise InventoryError("bootstrap method could not be resolved")
    return normalized


def _empty_inventory() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "machines": []}


def load_inventory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_inventory()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InventoryError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InventoryError(f"inventory root must be an object: {path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise InventoryError(
            f"unsupported schema_version in {path}: {data.get('schema_version')!r}"
        )
    machines = data.get("machines")
    if not isinstance(machines, list):
        raise InventoryError(f"inventory machines must be a list: {path}")
    for idx, record in enumerate(machines):
        _validate_record(record, where=f"machines[{idx}]")
    return data


def _atomic_write_json(path: Path, data: Any) -> None:
    ensure_state_dir(path.parent)
    handle, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def save_inventory(path: Path, inventory: dict[str, Any]) -> None:
    _atomic_write_json(path, inventory)


@contextlib.contextmanager
def inventory_lock(
    path: Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
):
    ensure_state_dir(path.parent)
    lock_path = path.with_name(path.name + STATE_LOCK_SUFFIX)
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f'{os.getpid()}\n'.encode('utf-8'))
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise InventoryError(f'timed out waiting for inventory lock {lock_path}')
            time.sleep(poll_seconds)
    try:
        yield lock_path
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def _validate_record(record: Any, where: str = "record") -> None:
    if not isinstance(record, dict):
        raise InventoryError(f"{where} must be an object")
    alias = record.get("alias")
    if not isinstance(alias, str) or not alias.strip():
        raise InventoryError(f"{where}.alias must be a non-empty string")

    namespace = record.get("namespace")
    if namespace is not None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise InventoryError(f"{where}.namespace must be a non-empty string when present")
        record["namespace"] = validate_machine_username(namespace)

    host = record.get("host")
    if not isinstance(host, dict):
        raise InventoryError(f"{where}.host must be an object")
    host_ip = host.get("ip")
    host_user = host.get("user")
    host_port = host.get("port")
    if not isinstance(host_ip, str) or not host_ip.strip():
        raise InventoryError(f"{where}.host.ip must be a non-empty string")
    if not isinstance(host_user, str) or not host_user.strip():
        raise InventoryError(f"{where}.host.user must be a non-empty string")
    if not isinstance(host_port, int) or host_port <= 0:
        raise InventoryError(f"{where}.host.port must be a positive integer")

    container = record.get("container")
    if not isinstance(container, dict):
        raise InventoryError(f"{where}.container must be an object")
    container_name = container.get("name")
    container_port = container.get("ssh_port")
    image = container.get("image")
    workdir = container.get("workdir")
    if not isinstance(container_name, str) or not container_name.strip():
        raise InventoryError(f"{where}.container.name must be a non-empty string")
    if not isinstance(container_port, int) or container_port <= 0:
        raise InventoryError(f"{where}.container.ssh_port must be a positive integer")
    if not isinstance(image, str) or not image.strip():
        raise InventoryError(f"{where}.container.image must be a non-empty string")
    if not isinstance(workdir, str) or not workdir.strip():
        raise InventoryError(f"{where}.container.workdir must be a non-empty string")

    bootstrap_method = normalize_bootstrap_method(record.get("bootstrap_method"))
    if bootstrap_method is not None:
        record["bootstrap_method"] = bootstrap_method

    for key in ("managed_by_skill", "created_by_skill"):
        value = record.get(key)
        if value is not None and not isinstance(value, bool):
            raise InventoryError(f"{where}.{key} must be boolean when present")

    last_verified_at = record.get("last_verified_at")
    if last_verified_at is not None and not isinstance(last_verified_at, str):
        raise InventoryError(f"{where}.last_verified_at must be a string when present")


def _find_matches(
    inventory: dict[str, Any],
    identifier: str | None = None,
    *,
    alias: str | None = None,
    host_ip: str | None = None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for record in inventory["machines"]:
        if identifier is not None and (
            record["alias"] == identifier or record["host"]["ip"] == identifier
        ):
            matches.append(record)
            continue
        if alias is not None and record["alias"] == alias:
            matches.append(record)
            continue
        if host_ip is not None and record["host"]["ip"] == host_ip:
            matches.append(record)
    return matches


def preferred_inventory_path(path: Path) -> Path:
    return path.expanduser().resolve()


def read_inventory_path(path: Path) -> Path:
    requested = preferred_inventory_path(path)
    return resolve_inventory_read_path(requested)


def cmd_summary(args: argparse.Namespace) -> int:
    requested_path = preferred_inventory_path(args.inventory)
    active_path = read_inventory_path(requested_path)
    inventory = load_inventory(active_path)
    summary = {
        "schema_version": inventory["schema_version"],
        "inventory": str(active_path),
        "preferred_inventory": str(requested_path),
        "legacy_inventory": str(LEGACY_INVENTORY_PATH),
        "count": len(inventory["machines"]),
        "machines": [
            {
                "alias": record["alias"],
                "namespace": record.get("namespace"),
                "host": f"{record['host']['user']}@{record['host']['ip']}:{record['host']['port']}",
                "container": record["container"]["name"],
                "container_ssh_port": record["container"]["ssh_port"],
                "image": record["container"]["image"],
                "last_verified_at": record.get("last_verified_at"),
            }
            for record in inventory["machines"]
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    inventory = load_inventory(read_inventory_path(args.inventory))
    matches = _find_matches(inventory, identifier=args.identifier)
    if not matches:
        raise InventoryError(f"no machine found for identifier: {args.identifier}")
    if len(matches) > 1:
        raise InventoryError(
            f"multiple machines matched {args.identifier!r}; use a unique alias or host IP"
        )
    print(json.dumps(matches[0], indent=2, ensure_ascii=False))
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    if not args.image:
        raise InventoryError(
            "--image is required; record an explicit `rc`-resolved, `main`, `stable`-resolved, or concrete non-latest image reference"
        )
    requested_path = preferred_inventory_path(args.inventory)
    with inventory_lock(requested_path):
        active_path = read_inventory_path(requested_path)
        inventory = load_inventory(active_path)
        alias_matches = _find_matches(inventory, alias=args.alias)
        ip_matches = _find_matches(inventory, host_ip=args.host_ip)

        alias_record = alias_matches[0] if alias_matches else None
        ip_record = ip_matches[0] if ip_matches else None
        if alias_record is not None and ip_record is not None and alias_record is not ip_record:
            raise InventoryError(
                "alias and host IP match different existing records; resolve the conflict manually"
            )

        target = alias_record or ip_record
        namespace = validate_machine_username(args.namespace) if args.namespace else None
        record = {
            "alias": args.alias,
            "namespace": namespace,
            "host": {
                "ip": args.host_ip,
                "port": args.host_port,
                "user": args.host_user,
            },
            "container": {
                "name": args.container_name,
                "ssh_port": args.container_ssh_port,
                "image": args.image,
                "workdir": args.workdir,
            },
            "bootstrap_method": resolve_bootstrap_method(args.bootstrap_method, existing_record=target),
            "managed_by_skill": True,
            "created_by_skill": args.created_by_skill,
            "last_verified_at": args.last_verified_at,
        }
        if namespace is None:
            record.pop("namespace")
        _validate_record(record)

        if target is None:
            inventory["machines"].append(record)
            action = "inserted"
        else:
            target.clear()
            target.update(record)
            action = "updated"

        save_inventory(requested_path, inventory)
    payload = {
        "result": action,
        "alias": record["alias"],
        "namespace": record.get("namespace"),
        "host_ip": record["host"]["ip"],
        "inventory": str(requested_path),
    }
    if not same_path(active_path, requested_path):
        payload["loaded_from"] = str(active_path)
        payload["migrated_from_legacy"] = same_path(active_path, LEGACY_INVENTORY_PATH)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    requested_path = preferred_inventory_path(args.inventory)
    with inventory_lock(requested_path):
        active_path = read_inventory_path(requested_path)
        inventory = load_inventory(active_path)
        matches = _find_matches(inventory, identifier=args.identifier)
        if not matches:
            raise InventoryError(f"no machine found for identifier: {args.identifier}")
        if len(matches) > 1:
            raise InventoryError(
                f"multiple machines matched {args.identifier!r}; use a unique alias or host IP"
            )
        target = matches[0]
        inventory["machines"] = [record for record in inventory["machines"] if record is not target]
        save_inventory(requested_path, inventory)
    payload = {
        "result": "removed",
        "alias": target["alias"],
        "namespace": target.get("namespace"),
        "host_ip": target["host"]["ip"],
        "inventory": str(requested_path),
    }
    if not same_path(active_path, requested_path):
        payload["loaded_from"] = str(active_path)
        payload["migrated_from_legacy"] = same_path(active_path, LEGACY_INVENTORY_PATH)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=DEFAULT_PATH,
        help=f"inventory path (default: {DEFAULT_PATH})",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=lambda *args, **kwargs: argparse.ArgumentParser(*args, allow_abbrev=False, **kwargs),
    )

    summary = subparsers.add_parser("summary", help="print a concise inventory summary")
    summary.set_defaults(func=cmd_summary)

    get_cmd = subparsers.add_parser("get", help="print one machine record by alias or host IP")
    get_cmd.add_argument("identifier", help="machine alias or host IP")
    get_cmd.set_defaults(func=cmd_get)

    put_cmd = subparsers.add_parser("put", help="insert or update one machine record")
    put_cmd.add_argument("--alias", required=True)
    put_cmd.add_argument(
        "--namespace",
        "--machine-username",
        "--username",
        dest="namespace",
        help="stable workspace machine username used for collision-safe naming",
    )
    put_cmd.add_argument("--host-ip", "--host", dest="host_ip", required=True)
    put_cmd.add_argument("--host-port", "--host-ssh-port", dest="host_port", type=int, default=22)
    put_cmd.add_argument("--host-user", "--user", dest="host_user", default="root")
    put_cmd.add_argument("--container-name", "--name", dest="container_name", required=True)
    put_cmd.add_argument(
        "--container-ssh-port",
        "--container-port",
        "--port",
        dest="container_ssh_port",
        type=int,
        required=True,
    )
    put_cmd.add_argument(
        "--image",
        required=True,
    )
    put_cmd.add_argument("--workdir", default="/vllm-workspace")
    put_cmd.add_argument(
        "--bootstrap-method",
        choices=INPUT_BOOTSTRAP_METHOD_CHOICES,
        default="auto",
        help=(
            "bootstrap method; defaults to 'ssh' for new records and reuses the existing value when updating. "
            "'key' normalizes to 'ssh', 'password' to 'password-once'"
        ),
    )
    put_cmd.add_argument(
        "--created-by-skill",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="mark whether the container was created by the skill (default: true)",
    )
    put_cmd.add_argument("--last-verified-at")
    put_cmd.set_defaults(func=cmd_put)

    upsert_cmd = subparsers.add_parser("upsert", help="alias of put; insert or update one machine record")
    upsert_cmd.add_argument("--alias", required=True)
    upsert_cmd.add_argument(
        "--namespace",
        "--machine-username",
        "--username",
        dest="namespace",
        help="stable workspace machine username used for collision-safe naming",
    )
    upsert_cmd.add_argument("--host-ip", "--host", dest="host_ip", required=True)
    upsert_cmd.add_argument("--host-port", "--host-ssh-port", dest="host_port", type=int, default=22)
    upsert_cmd.add_argument("--host-user", "--user", dest="host_user", default="root")
    upsert_cmd.add_argument("--container-name", "--name", dest="container_name", required=True)
    upsert_cmd.add_argument(
        "--container-ssh-port",
        "--container-port",
        "--port",
        dest="container_ssh_port",
        type=int,
        required=True,
    )
    upsert_cmd.add_argument(
        "--image",
        required=True,
    )
    upsert_cmd.add_argument("--workdir", default="/vllm-workspace")
    upsert_cmd.add_argument(
        "--bootstrap-method",
        choices=INPUT_BOOTSTRAP_METHOD_CHOICES,
        default="auto",
        help=(
            "bootstrap method; defaults to 'ssh' for new records and reuses the existing value when updating. "
            "'key' normalizes to 'ssh', 'password' to 'password-once'"
        ),
    )
    upsert_cmd.add_argument(
        "--created-by-skill",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="mark whether the container was created by the skill (default: true)",
    )
    upsert_cmd.add_argument("--last-verified-at")
    upsert_cmd.set_defaults(func=cmd_put)

    remove = subparsers.add_parser("remove", help="remove one machine record by alias or host IP")
    remove.add_argument("identifier", help="machine alias or host IP")
    remove.set_defaults(func=cmd_remove)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except InventoryError as exc:
        print(f"inventory error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
