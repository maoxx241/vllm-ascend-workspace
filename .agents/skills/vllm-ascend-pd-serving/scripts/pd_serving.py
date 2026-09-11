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


from remote_dev.diagnostics import open_http, http_connection, http_failure
from vaws_coordinator.presentation import execution_summary
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
from _serving_start import build_serve_command  # noqa: E402

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
    order = config.get("startup_order", [item["name"] for item in services if isinstance(item, Mapping) and "name" in item])
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
        if proxy.get("proxy_mode", "direct") not in {"direct", "environment"}:
            errors.append("proxy.proxy_mode must be direct or environment")
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


def role_shell_command(service: Mapping[str, Any], *, preflight_only=False) -> str:
    extra = [str(item) for item in service.get("args") or []]
    return build_serve_command(
        model=str(service["model"]),
        served_model_name=str(service.get("served_model_name") or Path(service["model"]).name),
        tp=service.get("tp"),
        dp=service.get("dp"),
        extra_args=extra,
        preflight_only=preflight_only,
    )


def topology_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    roles = []
    for name in config.get("startup_order", [item["name"] for item in config["services"]]):
        service = next(item for item in config["services"] if item["name"] == name)
        npu_count = int(service.get("tp") or 1) * int(service.get("dp") or 1)
        role: dict[str, Any] = {
            "name": str(service["name"]),
            "npu_count": npu_count,
            "command": role_shell_command(service),
            "preflight": role_shell_command(service, preflight_only=True),
            "service_port": int(service["port"]) if service.get("port") is not None else 0,
        }
        env = role_env(service)
        if env:
            role["env"] = env
        if service.get("host"):
            role["host"] = str(service["host"])
        roles.append(role)
    return {"roles": roles}


def _view(observation):
    return execution_summary(observation)


def start(config_path: Path, *, client=None, context_file=None, restart=False):
    config = _load_json(config_path, "PD config")
    validate_config(config)
    topology = topology_from_config(config)
    client = client or task_client(context_file)
    reply = run_command(
        client, topology["roles"][0]["command"],
        environment=named_environment(extra=config.get("environment")),
        topology=topology, timeout_seconds=None,
        service=str(config.get("group_id") or "pd"), restart=restart,
    )
    return _view(reply)


def status(*, service="pd", execution_id=None, config_path=None, client=None, context_file=None, urlopen=None):
    client = client or task_client(context_file)
    observation = client.observe(execution_id, "status", service=None if execution_id else service)
    result = _view(observation)
    if observation["state"] not in RUNNING or config_path is None:
        return result
    config = _load_json(config_path, "PD config")
    validate_config(config)
    health_url = config["proxy"]["base_url"].rstrip("/") + "/" + config["proxy"].get("health_path", "/health").lstrip("/")
    mode = config["proxy"].get("proxy_mode", "direct")
    proxy = http_connection(health_url, proxy_mode=mode)
    try:
        opener = urlopen or (lambda request, **kw: open_http(request, proxy_mode=mode, **kw))
        with opener(health_url, timeout=5) as response:
            proxy.update(ok=200 <= response.status < 300, status_code=response.status)
    except (OSError, urllib.error.URLError) as exc:
        proxy.update(ok=False, failure=http_failure(exc))
    return {**result, "readiness": "ready" if proxy["ok"] else "unhealthy", "proxy": proxy}


def stop(*, service="pd", execution_id=None, force=False, client=None, context_file=None):
    client = client or task_client(context_file)
    return _view(client.observe(execution_id, "stop", force, service=None if execution_id else service))


def smoke(
    config_path: Path,
    *,
    urlopen: Callable[..., Any] | None = None,
    updated_at: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    config = _load_json(config_path, "PD config")
    validate_config(config)
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
    mode = config["proxy"].get("proxy_mode", "direct")
    connection = http_connection(request, proxy_mode=mode)
    try:
        opener = urlopen or (lambda request, **kw: open_http(request, proxy_mode=mode, **kw))
        with opener(request, timeout=config["smoke"].get("timeout", 120)) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = response.status
    except (OSError, urllib.error.URLError) as exc:
        result = {"status": "failed", "connection": connection, "failure": http_failure(exc), "completed_at": updated_at or utc_now()}
        if output_dir is not None:
            _atomic_write(output_dir / "smoke.json", result)
        return result
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw_body": body[:4000]}
    result = {
        "status": "passed" if 200 <= status_code < 300 else "failed",
        "connection": connection,
        "status_code": status_code,
        "response": payload,
        "completed_at": updated_at or utc_now(),
        "claim": "proxy request path passed; inspect service logs to confirm connector-level KV transfer",
    }
    if output_dir is not None:
        _atomic_write(output_dir / "smoke.json", result)
    return result

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--config", required=True, type=Path)
    start_parser.add_argument("--restart", action="store_true")
    start_parser.add_argument("--context-file")
    for action in ("status", "stop"):
        command = commands.add_parser(action)
        reference = command.add_mutually_exclusive_group()
        reference.add_argument("--execution-id")
        reference.add_argument("--service", default="pd")
        command.add_argument("--context-file")
        if action == "status":
            command.add_argument("--config", type=Path, help="Optional proxy health configuration")
        else:
            command.add_argument("--force", action="store_true")
    smoke_parser = commands.add_parser("smoke")
    smoke_parser.add_argument("--config", required=True, type=Path)
    smoke_parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.action == "start":
            result = start(args.config, context_file=args.context_file, restart=args.restart)
        elif args.action == "status":
            result = status(service=args.service, execution_id=args.execution_id,
                            config_path=args.config, context_file=args.context_file)
        elif args.action == "stop":
            result = stop(service=args.service, execution_id=args.execution_id,
                          force=args.force, context_file=args.context_file)
        else:
            result = smoke(args.config, output_dir=args.output_dir)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status", result.get("state")) in {"failed", "timeout"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
