from __future__ import annotations

from typing import Any

from .benchmark_execution import describe_benchmark_preset, run_benchmark_probe, service_is_reusable
from .config import RepoPaths
from .serving_session import load_service_session
from .vaws_compat import unexpected_subcommand


def run_benchmark(
    paths: RepoPaths,
    server_name: str,
    preset_name: str,
    service_id: str | None,
) -> int:
    describe_benchmark_preset(preset_name)
    if not service_id:
        print("benchmark run requires --service-id")
        return 1

    service = load_service_session(paths, service_id)
    if not service_is_reusable(paths, server_name, service):
        print("service fingerprint does not match current workspace state")
        return 1

    result = run_benchmark_probe(
        paths,
        server_name=str(service["server_name"]),
        preset_name=preset_name,
        service_id=str(service["service_id"]),
    )
    print(str(result["summary"]))
    return 0


def run(paths: RepoPaths, args: Any) -> int:
    if args.benchmark_command == "run":
        return run_benchmark(paths, args.server_name, args.preset, args.service_id)
    raise unexpected_subcommand("benchmark", args.benchmark_command)
