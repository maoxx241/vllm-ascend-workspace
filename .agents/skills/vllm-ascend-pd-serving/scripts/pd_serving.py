#!/usr/bin/env python3
"""Operate a Session Group as a vLLM Ascend prefill/decode deployment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
ROLES = {"prefill", "decode"}


class PdServingError(ValueError):
    """Raised when a PD deployment config or lifecycle result is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PdServingError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PdServingError(f"{label} root must be an object")
    return payload


def validate_config(config: Mapping[str, Any], group: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("run_id", "group_id"):
        if not isinstance(config.get(field), str) or not config[field]:
            errors.append(f"{field} must be a non-empty string")
    if config.get("group_id") != group.get("group_id"):
        errors.append("config group_id does not match Session Group")
    if group.get("status") != "ready":
        errors.append(f"Session Group must be ready, got {group.get('status')}")
    raw_group_members = group.get("members")
    group_members: dict[str, Mapping[str, Any]] = {}
    session_ids: set[str] = set()
    snapshot_keys: set[str] = set()
    if not isinstance(raw_group_members, list) or not raw_group_members:
        errors.append("Session Group members must be a non-empty array")
        raw_group_members = []
    for index, member in enumerate(raw_group_members):
        path = f"Session Group members[{index}]"
        if not isinstance(member, Mapping):
            errors.append(f"{path} must be an object")
            continue
        name = member.get("name")
        session_id = member.get("session_id")
        snapshot = member.get("snapshot")
        if not isinstance(name, str) or not name:
            errors.append(f"{path}.name must be a non-empty string")
        elif name in group_members:
            errors.append(f"Session Group member name is duplicated: {name}")
        else:
            group_members[name] = member
        if not isinstance(session_id, str) or not session_id:
            errors.append(f"{path}.session_id must be a non-empty string")
        elif session_id in session_ids:
            errors.append(
                f"Session Group session_id is duplicated: {session_id}"
            )
        else:
            session_ids.add(session_id)
        if not isinstance(snapshot, Mapping) or not snapshot:
            errors.append(f"{path}.snapshot must be a non-empty object")
        else:
            snapshot_keys.add(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    if len(snapshot_keys) > 1:
        errors.append("Session Group members must share one code snapshot")
    services = config.get("services")
    if not isinstance(services, list) or len(services) < 2:
        errors.append("services must contain at least one prefill and one decode")
        services = []
    names: set[str] = set()
    members: set[str] = set()
    roles: set[str] = set()
    for index, service in enumerate(services):
        path = f"services[{index}]"
        if not isinstance(service, Mapping):
            errors.append(f"{path} must be an object")
            continue
        name = service.get("name")
        member = service.get("member")
        role = service.get("role")
        if not isinstance(name, str) or not name:
            errors.append(f"{path}.name must be a non-empty string")
        elif name in names:
            errors.append(f"service name is duplicated: {name}")
        else:
            names.add(name)
        if not isinstance(member, str) or member not in group_members:
            errors.append(f"{path}.member must name a Session Group member")
        elif member in members:
            errors.append(f"Session Group member is reused by multiple services: {member}")
        else:
            members.add(member)
        if role not in ROLES:
            errors.append(f"{path}.role must be prefill or decode")
        else:
            roles.add(role)
        if not isinstance(service.get("model"), str) or not service["model"]:
            errors.append(f"{path}.model must be a non-empty string")
        for field in ("tp", "dp", "port", "health_timeout"):
            value = service.get(field)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                errors.append(f"{path}.{field} must be a positive integer")
        if not isinstance(service.get("env", {}), Mapping):
            errors.append(f"{path}.env must be an object")
        if not isinstance(service.get("args", []), list) or any(
            not isinstance(value, str) for value in service.get("args", [])
        ):
            errors.append(f"{path}.args must be an array of strings")
    if roles != ROLES:
        errors.append("services must include both prefill and decode roles")
    order = config.get("startup_order")
    if (
        not isinstance(order, list)
        or any(not isinstance(value, str) for value in order)
        or len(order) != len(set(order))
        or set(order) != names
    ):
        errors.append("startup_order must contain every service name exactly once")
    connector = config.get("connector")
    if not isinstance(connector, Mapping):
        errors.append("connector must be an object")
    else:
        if connector.get("type") not in {"nixl", "mooncake", "custom"}:
            errors.append("connector.type must be nixl, mooncake, or custom")
        if not isinstance(connector.get("options"), Mapping):
            errors.append("connector.options must be an object")
    proxy = config.get("proxy")
    if not isinstance(proxy, Mapping):
        errors.append("proxy must be an object")
    else:
        if not isinstance(proxy.get("base_url"), str) or not proxy["base_url"]:
            errors.append("proxy.base_url must be a non-empty string")
        if not isinstance(proxy.get("health_path", "/health"), str):
            errors.append("proxy.health_path must be a string")
    smoke = config.get("smoke")
    if not isinstance(smoke, Mapping):
        errors.append("smoke must be an object")
    else:
        if not isinstance(smoke.get("path"), str) or not smoke["path"]:
            errors.append("smoke.path must be a non-empty string")
        if not isinstance(smoke.get("request"), Mapping):
            errors.append("smoke.request must be an object")
    if errors:
        raise PdServingError("; ".join(errors))


def service_command(
    service: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    action: str,
    force: bool = False,
) -> list[str]:
    serving = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
    if action == "start":
        command = [
            sys.executable,
            str(serving / "serve_start.py"),
            "--session-id",
            member["session_id"],
            "--model",
            service["model"],
        ]
        for field, option in (
            ("tp", "--tp"),
            ("dp", "--dp"),
            ("port", "--port"),
            ("health_timeout", "--health-timeout"),
        ):
            if service.get(field) is not None:
                command.extend([option, str(service[field])])
        for key, value in sorted(service.get("env", {}).items()):
            command.extend(["--extra-env", f"{key}={value}"])
        if service.get("args"):
            command.append("--")
            command.extend(service["args"])
        return command
    command = [
        sys.executable,
        str(serving / f"serve_{action}.py"),
        "--session-id",
        member["session_id"],
    ]
    if action == "stop" and force:
        command.append("--force")
    return command


def _run_json(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    completed = runner(
        list(command),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"stdout_tail": completed.stdout[-1000:]}
    return {
        "returncode": completed.returncode,
        "payload": payload,
        "stderr_tail": completed.stderr[-1000:],
        "command": list(command),
    }


def plan(
    output_dir: Path,
    *,
    config_path: Path,
    group_path: Path,
    created_at: str | None = None,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PdServingError(f"output directory is not empty: {output_dir}")
    config = _load_json(config_path, "PD config")
    group = _load_json(group_path, "Session Group")
    validate_config(config, group)
    members = {member["name"]: member for member in group["members"]}
    services = {service["name"]: service for service in config["services"]}
    lifecycle = []
    for name in config["startup_order"]:
        service = services[name]
        lifecycle.append(
            {
                "name": name,
                "role": service["role"],
                "member": service["member"],
                "session_id": members[service["member"]]["session_id"],
                "start_command": service_command(
                    service, members[service["member"]], action="start"
                ),
            }
        )
    timestamp = created_at or utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "pd-config.json", config)
    _atomic_write(output_dir / "session-group.json", group)
    _atomic_write(
        output_dir / "lifecycle.json",
        {
            "schema_version": SCHEMA_VERSION,
            "startup": lifecycle,
            "shutdown": list(reversed([row["name"] for row in lifecycle])),
        },
    )
    _atomic_write(
        output_dir / "state.json",
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": config["run_id"],
            "status": "planned",
            "started": [],
            "updated_at": timestamp,
        },
    )
    manifest = new_manifest(
        run_type="debug",
        run_id=config["run_id"],
        workspace_snapshot=group["members"][0]["snapshot"],
        topology={
            "session_group": group["group_id"],
            "services": [
                {
                    "name": service["name"],
                    "role": service["role"],
                    "member": service["member"],
                }
                for service in config["services"]
            ],
        },
        model={"paths": sorted({service["model"] for service in config["services"]})},
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("pd-config", "pd-config", "pd-config.json"),
        ("session-group", "session-group", "session-group.json"),
        ("lifecycle", "lifecycle", "lifecycle.json"),
        ("state", "state", "state.json"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "planned",
        "run_id": config["run_id"],
        "service_count": len(config["services"]),
        "startup_order": config["startup_order"],
    }


def start(
    output_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    updated_at: str | None = None,
) -> dict[str, Any]:
    config = _load_json(output_dir / "pd-config.json", "PD config")
    group = _load_json(output_dir / "session-group.json", "Session Group")
    state = _load_json(output_dir / "state.json", "PD state")
    if state["status"] != "planned":
        raise PdServingError(f"deployment must be planned, got {state['status']}")
    members = {member["name"]: member for member in group["members"]}
    services = {service["name"]: service for service in config["services"]}
    results: list[dict[str, Any]] = []
    started: list[str] = []
    failed: str | None = None
    for name in config["startup_order"]:
        service = services[name]
        member = members[service["member"]]
        result = _run_json(
            service_command(service, member, action="start"),
            runner=runner,
        )
        results.append({"name": name, **result})
        if result["returncode"] != 0 or result["payload"].get("status") != "ready":
            failed = name
            break
        started.append(name)
    rollback: list[dict[str, Any]] = []
    if failed:
        for name in reversed(started):
            service = services[name]
            member = members[service["member"]]
            rollback.append(
                {
                    "name": name,
                    **_run_json(
                        service_command(service, member, action="stop", force=True),
                        runner=runner,
                    ),
                }
            )
    timestamp = updated_at or utc_now()
    state.update(
        {
            "status": "failed" if failed else "running",
            "started": started,
            "failed_service": failed,
            "start_results": results,
            "rollback_results": rollback,
            "updated_at": timestamp,
        }
    )
    _atomic_write(output_dir / "state.json", state)
    manifest = load_manifest(output_dir / "manifest.json")
    manifest = transition_status(manifest, "running", updated_at=timestamp)
    if failed:
        manifest = transition_status(manifest, "failed", updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": state["status"],
        "started": started,
        "failed_service": failed,
        "rollback": [row["name"] for row in rollback],
    }


def status(
    output_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    config = _load_json(output_dir / "pd-config.json", "PD config")
    group = _load_json(output_dir / "session-group.json", "Session Group")
    members = {member["name"]: member for member in group["members"]}
    rows = []
    for service in config["services"]:
        rows.append(
            {
                "name": service["name"],
                **_run_json(
                    service_command(
                        service, members[service["member"]], action="status"
                    ),
                    runner=runner,
                ),
            }
        )
    health_url = (
        config["proxy"]["base_url"].rstrip("/")
        + "/"
        + config["proxy"].get("health_path", "/health").lstrip("/")
    )
    proxy: dict[str, Any]
    try:
        with urlopen(health_url, timeout=5) as response:
            proxy = {"ok": 200 <= response.status < 300, "status_code": response.status}
    except (OSError, urllib.error.URLError) as exc:
        proxy = {"ok": False, "error": str(exc)}
    ready = all(
        row["returncode"] == 0
        and row["payload"].get("status") in {"ready", "alive_healthy"}
        for row in rows
    ) and proxy["ok"]
    return {
        "status": "ready" if ready else "needs_repair",
        "services": rows,
        "proxy": {"url": health_url, **proxy},
    }


def smoke(
    output_dir: Path,
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    updated_at: str | None = None,
) -> dict[str, Any]:
    config = _load_json(output_dir / "pd-config.json", "PD config")
    url = (
        config["proxy"]["base_url"].rstrip("/")
        + "/"
        + config["smoke"]["path"].lstrip("/")
    )
    request_body = json.dumps(config["smoke"]["request"]).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=config["smoke"].get("timeout", 120)) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = response.status
    except (OSError, urllib.error.URLError) as exc:
        result = {"status": "failed", "url": url, "error": str(exc)}
        _atomic_write(output_dir / "smoke.json", result)
        return result
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw_body": body[:4000]}
    result = {
        "status": "passed" if 200 <= status_code < 300 else "failed",
        "url": url,
        "status_code": status_code,
        "response": payload,
        "completed_at": updated_at or utc_now(),
        "claim": "proxy request path passed; inspect service logs to confirm connector-level KV transfer",
    }
    _atomic_write(output_dir / "smoke.json", result)
    manifest = load_manifest(output_dir / "manifest.json")
    manifest = add_artifact(
        manifest,
        name="smoke",
        kind="pd-smoke",
        uri="smoke.json",
        updated_at=result["completed_at"],
    )
    write_manifest(output_dir / "manifest.json", manifest)
    return result


def stop(
    output_dir: Path,
    *,
    force: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    updated_at: str | None = None,
) -> dict[str, Any]:
    config = _load_json(output_dir / "pd-config.json", "PD config")
    group = _load_json(output_dir / "session-group.json", "Session Group")
    state = _load_json(output_dir / "state.json", "PD state")
    members = {member["name"]: member for member in group["members"]}
    services = {service["name"]: service for service in config["services"]}
    results = []
    for name in reversed(config["startup_order"]):
        service = services[name]
        results.append(
            {
                "name": name,
                **_run_json(
                    service_command(
                        service,
                        members[service["member"]],
                        action="stop",
                        force=force,
                    ),
                    runner=runner,
                ),
            }
        )
    success = all(
        row["returncode"] == 0
        and row["payload"].get("status") in {"stopped", "not_found"}
        for row in results
    )
    timestamp = updated_at or utc_now()
    state["status"] = "stopped" if success else "needs_repair"
    state["stop_results"] = results
    state["updated_at"] = timestamp
    _atomic_write(output_dir / "state.json", state)
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "running":
        manifest = transition_status(
            manifest, "passed" if success else "inconclusive", updated_at=timestamp
        )
        write_manifest(output_dir / "manifest.json", manifest)
    return {"status": state["status"], "stopped": [row["name"] for row in results]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
    plan_parser.add_argument("--group-file", required=True, type=Path)
    for name in ("start", "status", "smoke"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--output-dir", required=True, type=Path)
    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("--output-dir", required=True, type=Path)
    stop_parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "plan":
            payload = plan(
                args.output_dir,
                config_path=args.config,
                group_path=args.group_file,
            )
        elif args.action == "start":
            payload = start(args.output_dir)
        elif args.action == "status":
            payload = status(args.output_dir)
        elif args.action == "smoke":
            payload = smoke(args.output_dir)
        else:
            payload = stop(args.output_dir, force=args.force)
    except (PdServingError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
