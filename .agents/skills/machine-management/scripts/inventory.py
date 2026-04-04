#!/usr/bin/env python3
"""Manage local machine inventory for vllm-ascend-workspace.

This helper keeps the repo-root `.machine-inventory.json` file consistent.
It is intentionally small and dependency-free so agent workflows can rely on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_FILENAME = ".machine-inventory.json"
ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PATH = ROOT / DEFAULT_FILENAME


class InventoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MachineId:
    alias: str
    host_ip: str



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



def save_inventory(path: Path, inventory: dict[str, Any]) -> None:
    path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")



def _validate_record(record: Any, where: str = "record") -> None:
    if not isinstance(record, dict):
        raise InventoryError(f"{where} must be an object")
    alias = record.get("alias")
    if not isinstance(alias, str) or not alias.strip():
        raise InventoryError(f"{where}.alias must be a non-empty string")

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

    bootstrap_method = record.get("bootstrap_method")
    if bootstrap_method is not None and bootstrap_method not in {"ssh", "password-once"}:
        raise InventoryError(
            f"{where}.bootstrap_method must be 'ssh', 'password-once', or omitted"
        )

    for key in ("managed_by_skill", "created_by_skill"):
        value = record.get(key)
        if value is not None and not isinstance(value, bool):
            raise InventoryError(f"{where}.{key} must be boolean when present")

    last_verified_at = record.get("last_verified_at")
    if last_verified_at is not None and not isinstance(last_verified_at, str):
        raise InventoryError(f"{where}.last_verified_at must be a string when present")



def _iter_machine_ids(inventory: dict[str, Any]) -> list[MachineId]:
    return [
        MachineId(alias=record["alias"], host_ip=record["host"]["ip"])
        for record in inventory["machines"]
    ]



def _find_matches(inventory: dict[str, Any], identifier: str | None = None, *, alias: str | None = None, host_ip: str | None = None) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for record in inventory["machines"]:
        if identifier is not None and (record["alias"] == identifier or record["host"]["ip"] == identifier):
            matches.append(record)
            continue
        if alias is not None and record["alias"] == alias:
            matches.append(record)
            continue
        if host_ip is not None and record["host"]["ip"] == host_ip:
            matches.append(record)
    return matches



def cmd_summary(args: argparse.Namespace) -> int:
    inventory = load_inventory(args.inventory)
    summary = {
        "schema_version": inventory["schema_version"],
        "count": len(inventory["machines"]),
        "machines": [
            {
                "alias": record["alias"],
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
    inventory = load_inventory(args.inventory)
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
    inventory = load_inventory(args.inventory)
    alias_matches = _find_matches(inventory, alias=args.alias)
    ip_matches = _find_matches(inventory, host_ip=args.host_ip)

    alias_record = alias_matches[0] if alias_matches else None
    ip_record = ip_matches[0] if ip_matches else None
    if alias_record is not None and ip_record is not None and alias_record is not ip_record:
        raise InventoryError(
            "alias and host IP match different existing records; resolve the conflict manually"
        )

    target = alias_record or ip_record
    record = {
        "alias": args.alias,
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
        "bootstrap_method": args.bootstrap_method,
        "managed_by_skill": True,
        "created_by_skill": args.created_by_skill,
        "last_verified_at": args.last_verified_at,
    }
    _validate_record(record)

    if target is None:
        inventory["machines"].append(record)
        action = "inserted"
    else:
        target.clear()
        target.update(record)
        action = "updated"

    save_inventory(args.inventory, inventory)
    print(
        json.dumps(
            {
                "result": action,
                "alias": record["alias"],
                "host_ip": record["host"]["ip"],
                "inventory": str(args.inventory),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0



def cmd_remove(args: argparse.Namespace) -> int:
    inventory = load_inventory(args.inventory)
    matches = _find_matches(inventory, identifier=args.identifier)
    if not matches:
        raise InventoryError(f"no machine found for identifier: {args.identifier}")
    if len(matches) > 1:
        raise InventoryError(
            f"multiple machines matched {args.identifier!r}; use a unique alias or host IP"
        )
    target = matches[0]
    inventory["machines"] = [record for record in inventory["machines"] if record is not target]
    save_inventory(args.inventory, inventory)
    print(
        json.dumps(
            {
                "result": "removed",
                "alias": target["alias"],
                "host_ip": target["host"]["ip"],
                "inventory": str(args.inventory),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=DEFAULT_PATH,
        help=f"inventory path (default: {DEFAULT_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    summary = subparsers.add_parser("summary", help="print a concise inventory summary")
    summary.set_defaults(func=cmd_summary)

    get_cmd = subparsers.add_parser("get", help="print one machine record by alias or host IP")
    get_cmd.add_argument("identifier", help="machine alias or host IP")
    get_cmd.set_defaults(func=cmd_get)

    put_cmd = subparsers.add_parser("put", help="insert or update one machine record")
    put_cmd.add_argument("--alias", required=True)
    put_cmd.add_argument("--host-ip", required=True)
    put_cmd.add_argument("--host-port", type=int, default=22)
    put_cmd.add_argument("--host-user", default="root")
    put_cmd.add_argument("--container-name", required=True)
    put_cmd.add_argument("--container-ssh-port", type=int, required=True)
    put_cmd.add_argument(
        "--image",
        default="quay.nju.edu.cn/ascend/vllm-ascend:latest",
    )
    put_cmd.add_argument("--workdir", default="/vllm-workspace")
    put_cmd.add_argument(
        "--bootstrap-method",
        choices=["ssh", "password-once"],
        required=True,
    )
    put_cmd.add_argument(
        "--created-by-skill",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="mark whether the container was created by the skill (default: true)",
    )
    put_cmd.add_argument("--last-verified-at")
    put_cmd.set_defaults(func=cmd_put)

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
