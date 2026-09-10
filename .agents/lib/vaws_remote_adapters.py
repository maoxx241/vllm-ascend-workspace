"""Thin wrappers over serving/parity CLIs. Not a second scheduler or installer."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from vaws_local_state import ROOT
from vaws_remote_target import (
    RemoteTargetError,
    add_target_args,
    cli_error,
    duration_ms,
    now_iso,
    print_json,
    run_json_command,
    tail_text,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def known_hosts_status(endpoint: Any) -> dict[str, Any]:
    key = endpoint.known_hosts_key()
    result = subprocess.run(
        ["ssh-keygen", "-F", key],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "key": key,
        "present": result.returncode == 0 and bool(result.stdout.strip()),
        "stdout_tail": tail_text(result.stdout or "", 1000),
        "stderr_tail": tail_text(result.stderr or "", 1000),
    }


def _service_selector_args(args: Any) -> list[str]:
    flags: list[str] = []
    if getattr(args, "context_file", None):
        flags.extend(["--context-file", str(args.context_file)])
    if getattr(args, "execution_id", None):
        flags.extend(["--execution-id", str(args.execution_id)])
    if getattr(args, "service", None):
        flags.extend(["--service", str(args.service)])
    return flags


def call_service(action: str, args: Any, extra_args: list[str]) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    scripts = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
    script_map = {
        "start": scripts / "serve_start.py",
        "status": scripts / "serve_status.py",
        "stop": scripts / "serve_stop.py",
    }
    if action not in script_map:
        raise RemoteTargetError(f"unsupported service action: {action}")
    cmd = [sys.executable, str(script_map[action])]
    cmd.extend(_service_selector_args(args))
    cmd.extend(extra_args)
    rc, payload, stdout, stderr = run_json_command(cmd, relay_stderr=True)
    status = payload.get("status", "failed")
    if rc != 0 and status not in {"needs_input", "blocked", "failed", "timeout", "needs_repair", "cancelled"}:
        status = "failed"
    logs: dict[str, Any] = {}
    for key in ("log_stdout", "log_stderr", "runtime_dir"):
        if payload.get(key):
            logs[key] = payload[key]
    return {
        "status": status,
        "target": {"execution_id": getattr(args, "execution_id", None)},
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "action": action,
        "returncode": rc,
        "error": payload.get("error") if status != "ready" else None,
        "result": payload,
        "stdout_tail": tail_text(stdout),
        "stderr_tail": tail_text(stderr),
        "logs": logs,
    }


def service_logs(args: Any, *, lines: int = 120) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    from vaws_task_target import task_client

    if not getattr(args, "execution_id", None):
        return {
            "status": "needs_input",
            "target": {"execution_id": None},
            "started_at": started_at,
            "duration_ms": duration_ms(start),
            "error": "service logs require --execution-id",
            "logs": {},
        }
    observe_kwargs: dict[str, Any] = {}
    role = getattr(args, "role", None)
    if role:
        observe_kwargs["role"] = str(role)
    observation = task_client(getattr(args, "context_file", None)).observe(
        str(args.execution_id), "tail", **observe_kwargs
    )
    return {
        "status": "ok",
        "target": {"execution_id": args.execution_id, "role": role or None},
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "tail": observation.get("tail") or observation.get("stdout"),
        "result": observation,
        "logs": {},
    }


def _parity_command(args: Any, *, dry_run: bool = False) -> list[str]:
    """Project defaults for the package's explicit-endpoint source-only CLI."""
    if not getattr(args, "host", None):
        raise RemoteTargetError("direct source publication requires --host")
    source_root = Path(getattr(args, "repo_root", None) or ROOT).resolve()
    runtime_root = str(getattr(args, "runtime_root", None) or "/vllm-workspace")
    host = str(args.host)
    # This is a source-cache namespace, never a native task or execution id.
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", source_root.name.lower()).strip(".-") or "workspace"
    workspace_id = f"{label}-{hashlib.sha1(str(source_root).encode()).hexdigest()[:8]}"
    command = [
        sys.executable, "-m", "vaws_coordinator.parity", "sync",
        "--workspace-root", str(source_root),
        "--workspace-id", workspace_id,
        "--server-name", host,
        "--runtime-root", runtime_root,
        "--container-identity", f"{host}@{runtime_root}",
        "--container-host", host,
        "--container-port", str(getattr(args, "port", None) or 22),
        "--container-user", str(getattr(args, "user", None) or "root"),
        "--apply-mode", "source-only",
    ]
    for source in getattr(args, "source", None) or []:
        command.extend(["--source", str(source)])
    if dry_run:
        command.append("--dry-run")
    return command


def sync_plan(args: Any, *, mode: str, force_reinstall: bool = False) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    if getattr(args, "execution_id", None):
        return {
            "status": "blocked",
            "target": {"execution_id": args.execution_id},
            "started_at": started_at,
            "duration_ms": duration_ms(start),
            "error": (
                "coordinator prepares managed sources; do not synchronize or rebuild "
                "into a live execution root"
            ),
            "logs": {},
        }
    cmd = _parity_command(args)
    return {
        "status": "ok",
        "target": {"host": getattr(args, "host", None)},
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "mode": mode,
        "action": "source inspection only; managed preparation belongs to coordinator",
        "will_materialize": False,
        "will_install": False,
        "command": cmd,
        "logs": {},
    }


def sync_apply(
    args: Any,
    *,
    mode: str,
    force_reinstall: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    if getattr(args, "execution_id", None):
        return {
            "status": "blocked",
            "target": {"execution_id": args.execution_id},
            "started_at": started_at,
            "duration_ms": duration_ms(start),
            "error": (
                "coordinator prepares managed sources; do not synchronize or rebuild "
                "into a live execution root"
            ),
            "logs": {},
        }
    cmd = _parity_command(args, dry_run=dry_run)
    rc, payload, stdout, stderr = run_json_command(cmd, relay_stderr=True)
    status = payload.get("status", "failed")
    if rc != 0 and status not in {"blocked", "needs_input", "needs_repair", "timeout", "failed"}:
        status = "failed"
    return {
        "status": status,
        "target": {"host": getattr(args, "host", None)},
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "mode": mode,
        "returncode": rc,
        "result": payload,
        "stdout_tail": tail_text(stdout),
        "stderr_tail": tail_text(stderr),
        "logs": {},
    }


def cleanup(
    args: Any,
    *,
    dry_run: bool,
    jobs: bool,
    job_ids: Sequence[str] | None = None,
    service: bool,
    known_hosts: bool,
    remote_temp: bool,
    force: bool,
) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    actions: list[dict[str, Any]] = []
    status = "ok"
    if service:
        if dry_run:
            actions.append({"action": "service-stop", "dry_run": True})
        else:
            service_result = call_service("stop", args, ["--force"] if force else [])
            actions.append({"action": "service-stop", "result": service_result})
            if service_result.get("status") not in {"stopped", "not_found", "incomplete"}:
                status = str(service_result.get("status") or "failed")
    if jobs:
        from vaws_task_target import task_client

        execution_id = getattr(args, "execution_id", None)
        if not execution_id:
            return {
                "status": "needs_input",
                "error": "managed job stop requires --execution-id; generic process stop uses remote-dev",
                "started_at": started_at,
                "duration_ms": duration_ms(start),
                "actions": actions,
                "logs": {},
            }
        if dry_run:
            actions.append({"action": "execution-stop", "execution_id": execution_id, "dry_run": True})
        else:
            result = task_client(getattr(args, "context_file", None)).observe(str(execution_id), "stop", force)
            actions.append({"action": "execution-stop", "result": result})
    del job_ids, remote_temp, known_hosts
    return {
        "status": status,
        "target": {"execution_id": getattr(args, "execution_id", None)},
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "dry_run": dry_run,
        "actions": actions,
        "logs": {},
    }


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    add_target_args(parser)
    parser.add_argument("--repo-root", type=Path, default=ROOT, help="local source workspace")
    parser.add_argument("--runtime-root", default="/vllm-workspace", help="prepared direct target root")
    parser.add_argument("--source", action="append", default=[], help="actual business worktree, NAME=PATH")
    parser.add_argument("--mode", choices=("auto", "source-only"), default="source-only")


def cli_sync_plan(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan remote code sync without mutating runtime.", allow_abbrev=False)
    _add_source_args(parser)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = sync_plan(args, mode="source-only")
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_sync_apply(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply remote code sync in a selected mode.", allow_abbrev=False)
    _add_source_args(parser)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = sync_apply(
            args,
            mode="source-only",
            dry_run=args.dry_run,
        )
        print_json(payload)
        return 0 if payload["status"] in {"ready", "source-only", "materialized", "dry-run", "ok", "skipped"} else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_service_start(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start vLLM service through the remote serving skill.",
        epilog="All unrecognized options are passed through to serve_start.py, so both `-- --model /path` and `--model /path` work.",
        allow_abbrev=False,
    )
    add_target_args(parser)
    args, extra = parser.parse_known_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        extra = list(extra)
        if extra and extra[0] == "--":
            extra = extra[1:]
        payload = call_service("start", args, extra)
        print_json(payload)
        return 0 if payload["status"] in {"ready", "queued"} else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_service_status(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check vLLM service status.", allow_abbrev=False)
    add_target_args(parser)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = call_service("status", args, [])
        print_json(payload)
        return 0 if payload["status"] in {
            "ready", "alive", "alive_healthy", "stopped", "not_found", "queued",
        } else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_service_logs(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tail vLLM service logs.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--lines", type=int, default=120)
    parser.add_argument("--role", help="optional topology role for observe(action='tail', role=...)")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = service_logs(args, lines=args.lines)
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_service_stop(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop vLLM service.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = call_service("stop", args, ["--force"] if args.force else [])
        print_json(payload)
        return 0 if payload["status"] in {"stopped", "not_found"} else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_cleanup(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean remote session/service/temp state.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jobs", action="store_true", help="stop the coordinator execution named by --execution-id")
    parser.add_argument("--service", action="store_true", help="stop the named vLLM service")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        if args.all:
            args.jobs = args.service = True
        payload = cleanup(
            args,
            dry_run=args.dry_run,
            jobs=args.jobs,
            job_ids=None,
            service=args.service,
            known_hosts=False,
            remote_temp=False,
            force=args.force,
        )
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)
