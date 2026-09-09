#!/usr/bin/env python3
"""Group task-scoped service names for ordered multi-service work.

Members share the persistent user container. Each member names a business
service. Teardown does not delete the container.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

from vaws_local_state import ROOT as WORKSPACE_ROOT, utc_now_iso  # noqa: E402
from vaws_result_envelope import emit_skill_json  # noqa: E402
from vaws_session_id import normalize_session_id  # noqa: E402
from vaws_session_state import SessionStateError, require_task_id, write_json  # noqa: E402
from vaws_task_target import resolve_context_file  # noqa: E402

GROUP_SUBDIR = Path(".vaws-local") / "task-groups"


def print_json(data: dict[str, Any]) -> None:
    emit_skill_json(
        data,
        skill="session-management",
        entry_point=".agents/skills/session-management/scripts/session_group.py",
    )


def group_path(group_id: str, repo_root: Path | None = None) -> Path:
    root = repo_root if repo_root is not None else WORKSPACE_ROOT
    return root / GROUP_SUBDIR / require_task_id(group_id) / "group.json"


def parse_member(value: str) -> dict[str, str]:
    name, separator, service = value.partition("=")
    if not separator or not name.strip() or not service.strip():
        raise SessionStateError("member must be name=service")
    normalized_name = normalize_session_id(name.strip())
    if normalized_name is None:
        raise SessionStateError(f"invalid member name: {name!r}")
    return {"name": normalized_name, "service": service.strip()}


def load_group(group_id: str, repo_root: Path = WORKSPACE_ROOT) -> dict[str, Any]:
    path = group_path(group_id, repo_root)
    if not path.exists():
        raise SessionStateError(f"group {group_id!r} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_create(args: argparse.Namespace) -> int:
    gid = require_task_id(args.group_id)
    members = [parse_member(item) for item in args.member]
    names = [member["name"] for member in members]
    if len(names) != len(set(names)):
        raise SessionStateError("member names must be unique")
    services = [member["service"] for member in members]
    if len(services) != len(set(services)):
        raise SessionStateError("member services must be unique")
    context_file = resolve_context_file(args.context_file)
    order = [normalize_session_id(item) or item for item in (args.startup_order.split(",") if args.startup_order else names)]
    if set(order) != set(names):
        raise SessionStateError("startup-order must list every member exactly once")
    payload = {
        "schema_version": 1,
        "group_id": gid,
        "context_file": context_file,
        "members": members,
        "startup_order": order,
        "status": "ready",
        "created_at": utc_now_iso(),
    }
    path = group_path(gid)
    write_json(path, payload)
    print_json({"status": "ok", "group": payload, "group_file": str(path)})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    group = load_group(args.group_id)
    print_json({"status": "ok", "group": group})
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    root = WORKSPACE_ROOT / GROUP_SUBDIR
    groups = []
    if root.is_dir():
        for path in sorted(root.glob("*/group.json")):
            groups.append(json.loads(path.read_text(encoding="utf-8")))
    print_json({"status": "ok", "groups": groups})
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    from vaws_task_target import DONE, executions_for_service, task_client

    group = load_group(args.group_id)
    client = task_client(args.context_file or group.get("context_file"))
    names = [str(group["group_id"])]
    for member in group.get("members") or []:
        service = member.get("service") or member.get("name")
        if service and service not in names:
            names.append(str(service))
    stopped = []
    for service in names:
        for row in executions_for_service(client, service):
            eid = str(row.get("id") or row.get("execution_id") or "")
            if not eid:
                continue
            result = client.observe(eid, "stop", bool(getattr(args, "force", False)))
            stopped.append({
                "service": service,
                "execution_id": eid,
                "state": result.get("state"),
                "terminal": str(result.get("state") or "") in DONE,
            })
    print_json({
        "status": "ok" if all(item["terminal"] for item in stopped) or not stopped else "incomplete",
        "group_id": group["group_id"],
        "stopped": stopped,
        "container_preserved": True,
        "deleted": False,
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--group-id", required=True)
    create.add_argument("--member", action="append", required=True, help="name=service")
    create.add_argument("--startup-order", help="comma-separated member names")
    create.add_argument("--context-file")
    create.set_defaults(func=cmd_create)
    status = sub.add_parser("status")
    status.add_argument("--group-id", required=True)
    status.set_defaults(func=cmd_status)
    listing = sub.add_parser("list")
    listing.set_defaults(func=cmd_list)
    teardown = sub.add_parser("teardown")
    teardown.add_argument("--group-id", required=True)
    teardown.add_argument("--context-file")
    teardown.add_argument("--force", action="store_true")
    teardown.set_defaults(func=cmd_teardown)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
