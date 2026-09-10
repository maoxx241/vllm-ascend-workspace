#!/usr/bin/env python3
"""Operate one vLLM Ascend prefill/decode topology from its business config."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


from vaws_coordinator.code_identity import manifest_code  # noqa: E402
from vaws_coordinator.run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)
from vaws_task_target import (  # noqa: E402
    DONE,
    PENDING,
    RUNNING,
    named_environment,
    reject_reserved_env,
    run_command,
    task_client,
    task_id_of,
)

SERVING_SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
if str(SERVING_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SERVING_SCRIPTS))
from serve_start import build_serve_command  # noqa: E402

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


def validate_config(config: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("run_id", "group_id"):
        if not isinstance(config.get(field), str) or not config[field]:
            errors.append(f"{field} must be a non-empty string")
    services = config.get("services")
    if not isinstance(services, list) or len(services) < 2:
        errors.append("services must contain at least one prefill and one decode")
        services = []
    names: set[str] = set()
    roles: set[str] = set()
    for index, service in enumerate(services):
        path = f"services[{index}]"
        if not isinstance(service, Mapping):
            errors.append(f"{path} must be an object")
            continue
        name = service.get("name")
        role = service.get("role")
        if not isinstance(name, str) or not name:
            errors.append(f"{path}.name must be a non-empty string")
        elif name in names:
            errors.append(f"service name is duplicated: {name}")
        else:
            names.add(name)
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


def role_env(service: Mapping[str, Any]) -> dict[str, str]:
    return reject_reserved_env({str(k): str(v) for k, v in dict(service.get("env") or {}).items()})


def role_shell_command(service: Mapping[str, Any]) -> str:
    extra = [str(item) for item in service.get("args") or []]
    return build_serve_command(
        model=str(service["model"]),
        served_model_name=str(service.get("served_model_name") or Path(service["model"]).name),
        tp=service.get("tp"),
        dp=service.get("dp"),
        extra_args=extra,
    )


def topology_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    roles = []
    for name in config["startup_order"]:
        service = next(item for item in config["services"] if item["name"] == name)
        npu_count = int(service.get("tp") or 1) * int(service.get("dp") or 1)
        role: dict[str, Any] = {
            "name": str(service["name"]),
            "npu_count": npu_count,
            "command": role_shell_command(service),
            "service_port": int(service["port"]) if service.get("port") is not None else 0,
        }
        env = role_env(service)
        if env:
            role["env"] = env
        if service.get("host"):
            role["host"] = str(service["host"])
        roles.append(role)
    return {"roles": roles}


def plan(
    output_dir: Path,
    *,
    config_path: Path,
    created_at: str | None = None,
    code: Mapping[str, Any] | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PdServingError(f"output directory is not empty: {output_dir}")
    config = _load_json(config_path, "PD config")
    validate_config(config)
    services = {service["name"]: service for service in config["services"]}
    topology = topology_from_config(config)
    lifecycle = []
    for name in config["startup_order"]:
        service = services[name]
        lifecycle.append(
            {
                "name": name,
                "role": service["role"],
            }
        )
    timestamp = created_at or utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "pd-config.json", config)
    _atomic_write(
        output_dir / "lifecycle.json",
        {
            "schema_version": SCHEMA_VERSION,
            "startup": lifecycle,
            "shutdown": list(reversed([row["name"] for row in lifecycle])),
            "topology": topology,
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
        code=code or manifest_code(workspace_root or ROOT),
        workspace_root=workspace_root or ROOT,
        workspace_snapshot=code or manifest_code(workspace_root or ROOT),
        topology={
            "service": config["group_id"],
            "roles": topology["roles"],
            "services": [
                {
                    "name": service["name"],
                    "role": service["role"],
                }
                for service in config["services"]
            ],
        },
        model={"paths": sorted({service["model"] for service in config["services"]})},
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("pd-config", "pd-config", "pd-config.json"),
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
    client: Any | None = None,
    context_file: str | None = None,
    updated_at: str | None = None,
    restart: bool = False,
) -> dict[str, Any]:
    config = _load_json(output_dir / "pd-config.json", "PD config")
    state = _load_json(output_dir / "state.json", "PD state")
    if state["status"] not in {"planned", "queued"}:
        raise PdServingError(f"deployment must be planned or queued, got {state['status']}")
    topology = topology_from_config(config)
    roles = topology["roles"]
    if not roles:
        raise PdServingError("PD topology has no roles")
    client = client or task_client(context_file)
    environment = named_environment(extra=config.get("environment"))
    reply = run_command(
        client,
        roles[0]["command"],
        environment=environment,
        topology=topology,
        timeout_seconds=None,
        service=str(config["group_id"]),
        restart=restart,
    )
    timestamp = updated_at or utc_now()
    execution_id = reply.get("execution_id")
    run_state = str(reply.get("state") or "")
    if run_state in PENDING:
        status = "queued"
    elif run_state in RUNNING:
        status = "running"
    elif run_state in DONE:
        status = "failed"
    else:
        status = "queued"
    state.update(
        {
            "status": status,
            "execution_id": execution_id,
            "task_id": task_id_of(client),
            "service": config["group_id"],
            "state": run_state,
            "assignment": reply.get("assignment"),
            "roles": reply.get("roles"),
            "result": reply,
            "updated_at": timestamp,
        }
    )
    _atomic_write(output_dir / "state.json", state)
    manifest = load_manifest(output_dir / "manifest.json")
    if status == "running":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
    elif status == "failed":
        manifest = transition_status(manifest, "failed", updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": status,
        "execution_id": execution_id,
        "state": run_state,
        "service": config["group_id"],
        "running": status == "running",
        "ready": False,
        "result": reply,
    }


def status(
    output_dir: Path,
    *,
    client: Any | None = None,
    context_file: str | None = None,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    config = _load_json(output_dir / "pd-config.json", "PD config")
    state = _load_json(output_dir / "state.json", "PD state")
    execution_id = state.get("execution_id")
    if not execution_id:
        return {"status": "not_found", "error": "no coordinator execution for this PD run"}
    client = client or task_client(context_file)
    observation = client.observe(str(execution_id), "status")
    run_state = str(observation.get("state") or "")
    if run_state in PENDING:
        return {
            "status": "queued",
            "execution_id": execution_id,
            "state": run_state,
            "running": False,
            "ready": False,
            "observation": observation,
        }
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
    ready = run_state in RUNNING and proxy["ok"]
    return {
        "status": "ready" if ready else "needs_repair",
        "execution_id": execution_id,
        "state": run_state,
        "running": run_state in RUNNING,
        "ready": ready,
        "observation": observation,
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
    client: Any | None = None,
    context_file: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    state = _load_json(output_dir / "state.json", "PD state")
    execution_id = state.get("execution_id")
    if not execution_id:
        return {"status": "not_found", "container_preserved": True}
    client = client or task_client(context_file)
    result = client.observe(str(execution_id), "stop", force)
    run_state = str(result.get("state") or "")
    success = run_state in DONE
    timestamp = updated_at or utc_now()
    state["status"] = "stopped" if success else "needs_repair"
    state["state"] = run_state
    state["stop_result"] = result
    state["updated_at"] = timestamp
    _atomic_write(output_dir / "state.json", state)
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "running":
        manifest = transition_status(
            manifest, "passed" if success else "inconclusive", updated_at=timestamp
        )
        write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": state["status"],
        "execution_id": execution_id,
        "state": run_state,
        "container_preserved": True,
        "result": result,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--output-dir", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
    plan_parser.add_argument("--context-file")
    for name in ("start", "status", "smoke"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--output-dir", required=True, type=Path)
        subparser.add_argument("--context-file")
    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("--output-dir", required=True, type=Path)
    stop_parser.add_argument("--force", action="store_true")
    stop_parser.add_argument("--context-file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "plan":
            payload = plan(
                args.output_dir,
                config_path=args.config,
                code=manifest_code(ROOT),
            )
        elif args.action == "start":
            payload = start(args.output_dir, context_file=args.context_file)
        elif args.action == "status":
            payload = status(args.output_dir, context_file=args.context_file)
        elif args.action == "smoke":
            payload = smoke(args.output_dir)
        else:
            payload = stop(args.output_dir, force=args.force, context_file=args.context_file)
    except (PdServingError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
