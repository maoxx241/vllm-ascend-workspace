"""Pass native task context into the coordinator package client.

This is not a request ledger, allocator, or recovery manager.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

DONE = frozenset({"succeeded", "failed", "timeout", "cancelled", "inconclusive"})
PENDING = frozenset({
    "waiting_for_runtime",
    "queued",
    "waiting",
    "preparing",
    "starting",
    "uncertain",
    "planned",
    "launch_pending",
    "bound",
    "blocked",
})
RUNNING = frozenset({"running", "active"})
RESERVED_LAUNCH_ENV = frozenset({"VAWS_SERVICE_PORT", "ASCEND_RT_VISIBLE_DEVICES", "VAWS_PYTHON", "VAWS_EXECUTION_OBSERVATION"})


class TaskTargetError(RuntimeError):
    """Missing native context or package client refusal."""


def resolve_context_file(explicit: str | None = None) -> str:
    from vaws_managed_entry import require_managed_owner
    from vaws_coordinator.agent_session import load_context

    try:
        require_managed_owner(Path(__file__).resolve().parents[2])
        return load_context(explicit or "")["context_file"]
    except (ValueError, RuntimeError) as exc:
        raise TaskTargetError(str(exc)) from exc


def task_client(context_file: str | None = None, **kwargs: Any) -> Any:
    from vaws_coordinator.task_client import TaskClient

    return TaskClient(resolve_context_file(context_file), **kwargs)


def task_id_of(client: Any) -> str:
    return str(client.context["session"]["id"])


def add_task_args(parser: argparse.ArgumentParser | argparse._ArgumentGroup) -> None:
    parser.add_argument(
        "--context-file",
        help="native VAWS task context; defaults to VAWS_CONTEXT_FILE",
    )
    parser.add_argument("--execution-id", help="coordinator execution id when already known")
    parser.add_argument(
        "--service",
        default="",
        help="task-scoped business name for a long-running service",
    )


def reject_reserved_env(env: dict[str, str] | None) -> dict[str, str]:
    cleaned = dict(env or {})
    reserved = sorted(key for key in cleaned if key in RESERVED_LAUNCH_ENV)
    if reserved:
        raise TaskTargetError(
            "do not set reserved launch environment "
            + ", ".join(reserved)
            + "; the coordinator injects VAWS_PYTHON, VAWS_SERVICE_PORT, and "
            "ASCEND_RT_VISIBLE_DEVICES from the selected environment"
        )
    return cleaned


def service_resources(
    *,
    npu_count: int | None = None,
    devices: list[int] | None = None,
    service_port: int | None = 0,
) -> dict[str, Any]:
    """Build TaskClient.run resources. Devices and npu_count are mutually exclusive."""
    resources: dict[str, Any] = {}
    if devices:
        resources["devices"] = list(devices)
    else:
        resources["npu_count"] = int(npu_count or 1)
    if service_port is not None:
        resources["service_port"] = int(service_port)
    return resources


ENVIRONMENT_CONSTRAINT_KEYS = ("recipe", "python_abi", "cann", "soc", "machine_type")


def named_environment(
    *,
    recipe: str | None = None,
    python_abi: str | None = None,
    cann: str | None = None,
    soc: str | None = None,
    machine_type: str | None = None,
    preset: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Forward environment constraints to TaskClient.run. Recipe is optional.

    Coordinator matching never silently ignores supplied constraints. This
    helper must not drop ``soc`` / ``machine_type`` / ABI / CANN when recipe
    is omitted.
    """
    raw: dict[str, Any] = {}
    preset_env = (preset or {}).get("environment")
    if isinstance(preset_env, dict):
        raw.update(preset_env)
    if preset:
        for key in ENVIRONMENT_CONSTRAINT_KEYS:
            if preset.get(key) not in (None, "") and key not in raw:
                raw[key] = preset[key]
    if extra:
        raw.update({key: value for key, value in extra.items() if value not in (None, "")})
    overrides = {
        "recipe": recipe,
        "python_abi": python_abi,
        "cann": cann,
        "soc": soc,
        "machine_type": machine_type,
    }
    for key, value in overrides.items():
        if value not in (None, ""):
            raw[key] = value
    out = {str(key): str(value) for key, value in raw.items() if value not in (None, "")}
    return out or None


def run_command(client: Any, command: str, **kwargs: Any) -> dict[str, Any]:
    """Submit one managed command. Association and recovery stay in the package."""
    return client.run(command, **kwargs)


def execution_target(client: Any, execution_id: str) -> dict[str, Any]:
    """Authoritative routing for one owned execution. No guessed host."""
    return client.target(execution_id)
