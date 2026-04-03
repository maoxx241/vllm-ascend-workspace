from __future__ import annotations

from typing import Any

from .config import RepoPaths
from .serving_lifecycle import start_service_session, stop_service_session
from .serving_session import describe_service_session, list_service_sessions
from .vaws_compat import emit_json, unexpected_subcommand


def list_services(paths: RepoPaths) -> int:
    for payload in list_service_sessions(paths):
        print(
            f"{payload['service_id']}\t{payload['server_name']}\t"
            f"{payload['primary_served_model_name']}\t{payload['lifecycle']}"
        )
    return 0


def start_service(
    paths: RepoPaths,
    server_name: str,
    preset_name: str,
    weights_path: str,
    api_key_env: str | None = None,
) -> int:
    service_id = start_service_session(
        paths,
        server_name=server_name,
        preset_name=preset_name,
        weights_path=weights_path,
        api_key_env=api_key_env,
        lifecycle="explicit-serving",
    )
    print(service_id)
    return 0


def status_service(paths: RepoPaths, service_id: str) -> int:
    emit_json(describe_service_session(paths, service_id))
    return 0


def stop_service(paths: RepoPaths, service_id: str) -> int:
    stop_service_session(paths, service_id)
    return 0


def run(paths: RepoPaths, args: Any) -> int:
    if args.serving_command == "list":
        return list_services(paths)
    if args.serving_command == "start":
        return start_service(paths, args.server_name, args.preset, args.weights_path, args.api_key_env)
    if args.serving_command == "status":
        return status_service(paths, args.service_id)
    if args.serving_command == "stop":
        return stop_service(paths, args.service_id)
    raise unexpected_subcommand("serving", args.serving_command)
