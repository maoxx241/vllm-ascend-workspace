#!/usr/bin/env python3
"""Use the optional host-shared NPU task coordination queue.

This wrapper resolves a managed machine/session to its bare-metal host, sends
the stdlib-only coordinator implementation there, and stores ephemeral shared
state under ``/tmp/vaws-npu-coordinator/v1`` by default.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_npu_coordination import (  # noqa: E402
    DEFAULT_ESTIMATED_DURATION_SECONDS,
    DEFAULT_GRANT_TTL_SECONDS,
    DEFAULT_HEARTBEAT_TTL_SECONDS,
    DEFAULT_QUEUE_TTL_SECONDS,
    DEFAULT_START_TTL_SECONDS,
    DEFAULT_STATE_DIR,
)
from vaws_local_state import ensure_workspace_identity, load_workspace_identity  # noqa: E402

PROGRESS_SENTINEL = "__VAWS_NPU_COORDINATION_PROGRESS__="


class LocalEndpoint:
    def __init__(self, *, host: str, port: int, user: str) -> None:
        self.host = host
        self.port = port
        self.user = user

    def destination(self) -> str:
        return f"{self.user}@{self.host}"


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def emit_progress(phase: str, message: str, **extra: Any) -> None:
    payload = {"phase": phase, "message": message, **extra}
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


def configure_local_ssh_path() -> None:
    shim_dir = ROOT / ".vaws-local" / "bin"
    ssh_shim = shim_dir / "ssh"
    if ssh_shim.exists() and os.access(ssh_shim, os.X_OK):
        os.environ["PATH"] = f"{shim_dir}:{os.environ.get('PATH', '')}"


def resolve_target(args: argparse.Namespace) -> tuple[str, LocalEndpoint, dict[str, Any]]:
    if args.host:
        endpoint = LocalEndpoint(host=args.host, port=args.host_port, user=args.host_user)
        return args.host, endpoint, {
            "mode": "direct-host",
            "alias": args.host,
            "host": {"host": endpoint.host, "port": endpoint.port, "user": endpoint.user},
        }

    from vaws_remote_toolbox import resolve_remote_target

    target = resolve_remote_target(
        machine=args.machine,
        session_id=args.target_session_id,
        session_file=args.session_file,
        repo_root=ROOT,
    )
    endpoint = LocalEndpoint(
        host=target.host_endpoint.host,
        port=target.host_endpoint.port,
        user=target.host_endpoint.user,
    )
    return target.alias, endpoint, {
        "mode": target.mode,
        "alias": target.alias,
        "target_id": target.target_id,
        "host": target.host_endpoint.to_dict(plane="host"),
        "container": {
            "name": target.container_name,
            "image_record": target.container_image,
            **target.container_endpoint.to_dict(plane="container"),
        },
    }


def build_remote_command(request: dict[str, Any]) -> str:
    source = (LIB_DIR / "vaws_npu_coordination.py").read_text(encoding="utf-8")
    request_json = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    delimiter = "__VAWS_NPU_COORDINATION_SOURCE__"
    runner = """
import sys as _sys
try:
    _request = json.loads(_sys.argv[1])
    _result = handle_request(_request)
    print(json.dumps(_result, indent=2, ensure_ascii=False, sort_keys=True))
except CoordinationError as _exc:
    print(json.dumps({"status": "needs_input", "error": str(_exc)}, indent=2, ensure_ascii=False, sort_keys=True))
    raise SystemExit(2)
except Exception as _exc:
    print(json.dumps({"status": "failed", "error": str(_exc)}, indent=2, ensure_ascii=False, sort_keys=True))
    raise SystemExit(2)
"""
    return (
        "set -euo pipefail\n"
        "if command -v python3 >/dev/null 2>&1; then _py=python3; "
        "elif command -v python >/dev/null 2>&1; then _py=python; "
        "else echo '{\"status\":\"failed\",\"error\":\"python not found on host\"}'; exit 127; fi\n"
        f"\"$_py\" - {shlex.quote(request_json)} <<'{delimiter}'\n"
        f"{source}\n{runner}\n{delimiter}\n"
    )


def ssh_execute(
    endpoint: LocalEndpoint,
    command: str,
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "ssh",
        "-T",
        "-n",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(endpoint.port),
        endpoint.destination(),
        "bash",
        "-c",
        shlex.quote(command),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--machine", help="managed machine alias or host IP")
    target.add_argument("--session-id", dest="target_session_id", help="managed session used to resolve the host")
    target.add_argument("--session-file", help="managed session file used to resolve the host")
    target.add_argument("--host", help="direct bare-metal host address")
    parser.add_argument("--host-port", type=int, default=22)
    parser.add_argument("--host-user", default="root")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--timeout", type=float, default=45.0)


def add_token_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--fence-token", required=True, type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    add_target_arguments(parser)
    subparsers = parser.add_subparsers(dest="action", required=True)

    submit = subparsers.add_parser("submit", help="publish one queued NPU task")
    submit.add_argument("--task-id")
    submit.add_argument("--agent-id", help="defaults to the persistent local workspace UUID")
    submit.add_argument("--agent-alias", help="defaults to the configured unified workspace alias")
    submit.add_argument("--task-session-id")
    submit.add_argument("--container-name")
    resources = submit.add_mutually_exclusive_group(required=True)
    resources.add_argument("--devices", help="exact physical device ids")
    resources.add_argument("--npu-count", type=int, help="number of devices")
    submit.add_argument("--not-before", help="timezone-aware ISO timestamp")
    latest = submit.add_mutually_exclusive_group()
    latest.add_argument("--latest-start", help="timezone-aware ISO timestamp")
    latest.add_argument("--queue-ttl-seconds", type=int, default=DEFAULT_QUEUE_TTL_SECONDS)
    submit.add_argument(
        "--estimated-duration-seconds",
        type=int,
        default=DEFAULT_ESTIMATED_DURATION_SECONDS,
    )
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--preemptible", action="store_true")
    submit.add_argument("--message")

    acquire = subparsers.add_parser("acquire", help="try to grant the FIFO queue head")
    acquire.add_argument("--task-id", required=True)
    acquire.add_argument("--grant-ttl-seconds", type=int, default=DEFAULT_GRANT_TTL_SECONDS)

    preflight = subparsers.add_parser("preflight", help="recheck real occupancy immediately before launch")
    add_token_arguments(preflight)
    preflight.add_argument("--start-ttl-seconds", type=int, default=DEFAULT_START_TTL_SECONDS)

    activate = subparsers.add_parser("activate", help="record the launched process as active")
    add_token_arguments(activate)
    activate.add_argument("--pid", type=int, required=True)
    activate.add_argument(
        "--heartbeat-ttl-seconds",
        type=int,
        default=DEFAULT_HEARTBEAT_TTL_SECONDS,
    )

    heartbeat = subparsers.add_parser("heartbeat", help="renew one active task heartbeat")
    add_token_arguments(heartbeat)
    heartbeat.add_argument(
        "--heartbeat-ttl-seconds",
        type=int,
        default=DEFAULT_HEARTBEAT_TTL_SECONDS,
    )

    release = subparsers.add_parser("release", help="release only after repeated free probes")
    add_token_arguments(release)
    release.add_argument("--free-samples", type=int, default=2)
    release.add_argument("--interval-seconds", type=float, default=2.0)

    cancel = subparsers.add_parser("cancel", help="cancel a queued task or quarantine a busy task")
    cancel.add_argument("--task-id", required=True)

    status = subparsers.add_parser("status", help="show queue, grants, holds, occupancy, and recent events")
    status.add_argument("--task-id")
    status.add_argument("--event-limit", type=int, default=50)
    status.add_argument("--no-probe", action="store_true")

    gc = subparsers.add_parser("gc", help="apply timeout and orphan reconciliation")
    gc.add_argument("--event-limit", type=int, default=50)
    gc.add_argument("--no-probe", action="store_true")

    hold_add = subparsers.add_parser("hold-add", help="publish a human/manual protected window")
    hold_add.add_argument("--hold-id")
    hold_add.add_argument("--owner", required=True)
    hold_add.add_argument("--devices", required=True)
    hold_add.add_argument("--from", dest="not_before")
    hold_end = hold_add.add_mutually_exclusive_group(required=True)
    hold_end.add_argument("--until", dest="end_at")
    hold_end.add_argument("--duration-seconds", type=int)
    hold_add.add_argument("--reason")

    hold_remove = subparsers.add_parser("hold-remove", help="cancel a manual hold")
    hold_remove.add_argument("--hold-id", required=True)
    return parser


def request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    request: dict[str, Any] = {"action": args.action, "state_dir": args.state_dir}
    if args.action == "submit":
        identity = load_workspace_identity()
        if args.agent_id is None and identity is None:
            identity, _ = ensure_workspace_identity()
        agent_id = args.agent_id or (identity or {}).get("agent_id")
        agent_alias = args.agent_alias
        if agent_alias is None and identity and identity.get("alias_decision") == "set":
            agent_alias = identity.get("alias")
        request.update(
            {
                "task_id": args.task_id,
                "agent_id": agent_id,
                "agent_alias": agent_alias,
                "session_id": args.task_session_id or args.target_session_id,
                "container_name": args.container_name,
                "devices": args.devices,
                "npu_count": args.npu_count,
                "not_before": args.not_before,
                "latest_start": args.latest_start,
                "queue_ttl_seconds": args.queue_ttl_seconds,
                "estimated_duration_seconds": args.estimated_duration_seconds,
                "priority": args.priority,
                "preemptible": args.preemptible,
                "message": args.message,
            }
        )
    elif args.action == "acquire":
        request.update({"task_id": args.task_id, "grant_ttl_seconds": args.grant_ttl_seconds})
    elif args.action == "preflight":
        request.update(
            {
                "task_id": args.task_id,
                "fence_token": args.fence_token,
                "start_ttl_seconds": args.start_ttl_seconds,
            }
        )
    elif args.action == "activate":
        request.update(
            {
                "task_id": args.task_id,
                "fence_token": args.fence_token,
                "pid": args.pid,
                "heartbeat_ttl_seconds": args.heartbeat_ttl_seconds,
            }
        )
    elif args.action == "heartbeat":
        request.update(
            {
                "task_id": args.task_id,
                "fence_token": args.fence_token,
                "heartbeat_ttl_seconds": args.heartbeat_ttl_seconds,
            }
        )
    elif args.action == "release":
        request.update(
            {
                "task_id": args.task_id,
                "fence_token": args.fence_token,
                "free_samples": args.free_samples,
                "interval_seconds": args.interval_seconds,
            }
        )
    elif args.action == "cancel":
        request["task_id"] = args.task_id
    elif args.action == "status":
        request.update(
            {"task_id": args.task_id, "event_limit": args.event_limit, "no_probe": args.no_probe}
        )
    elif args.action == "gc":
        request.update({"event_limit": args.event_limit, "no_probe": args.no_probe})
    elif args.action == "hold-add":
        request.update(
            {
                "hold_id": args.hold_id,
                "owner": args.owner,
                "devices": args.devices,
                "not_before": args.not_before,
                "end_at": args.end_at,
                "duration_seconds": args.duration_seconds,
                "reason": args.reason,
            }
        )
    elif args.action == "hold-remove":
        request["hold_id"] = args.hold_id
    return request


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_local_ssh_path()
        alias, endpoint, target = resolve_target(args)
        request = request_from_args(args)
        emit_progress(
            "resolve-target",
            f"coordinating on host {endpoint.destination()}:{endpoint.port}",
            action=args.action,
        )
        command = build_remote_command(request)
        timeout = max(
            args.timeout,
            30.0
            + (args.free_samples * args.interval_seconds if args.action == "release" else 0.0),
        )
        result = ssh_execute(endpoint, command, timeout=timeout)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            print_json(
                {
                    "status": "failed",
                    "error": f"remote coordinator did not return JSON: {exc}",
                    "returncode": result.returncode,
                    "stdout": result.stdout[-2000:],
                    "stderr": result.stderr[-2000:],
                    "machine": alias,
                    "target": target,
                }
            )
            return 2
        payload["machine"] = alias
        payload["target"] = target
        if result.stderr:
            payload["remote_stderr_tail"] = result.stderr[-2000:]
        print_json(payload)
        return 0 if result.returncode == 0 else 2
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
