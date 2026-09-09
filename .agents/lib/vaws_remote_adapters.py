"""Service, sync, and cleanup adapters that remote-dev v0.3.0 cannot express.

These spawn existing serving/parity/session CLIs. SSH, when needed, goes
through ``vaws_remote_dev``. This is not a second transport.
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from vaws_local_state import ROOT
from vaws_remote_dev import ssh_exec
from vaws_remote_target import (
    RemoteTarget,
    RemoteTargetError,
    add_target_args,
    cli_error,
    duration_ms,
    now_iso,
    print_json,
    run_json_command,
    tail_text,
    target_from_args,
)
from vaws_session_state import (
    release_all_session_leases,
    session_serving_state_path,
)
from vaws_validate import require_safe_id

VLLM_REINSTALL_PATTERNS = (
    "requirements*",
    "pyproject.toml",
    "setup.*",
    "CMake*",
    "cmake/**",
    "csrc/**",
    "*.c",
    "*.cc",
    "*.cpp",
    "*.cu",
    "*.cuh",
    "*.h",
    "*.hpp",
)
VLLM_ASCEND_REINSTALL_PATTERNS = (
    *VLLM_REINSTALL_PATTERNS,
    "vllm_ascend/_cann_ops_custom/**",
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


def _load_serving_state_for_target(target: RemoteTarget) -> tuple[dict[str, Any] | None, Path]:
    if not target.session_id:
        raise RemoteTargetError(
            "serving state is session-scoped; resolve the target through a session "
            "(--session-id/--session-file or a bound worktree)"
        )
    path = session_serving_state_path(target.session_id, target.state_repo_root)
    if not path.exists():
        return None, path
    try:
        return _load_json(path), path
    except Exception:
        return None, path


def call_service(action: str, target: RemoteTarget, extra_args: list[str]) -> dict[str, Any]:
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
    if not target.session_id:
        raise RemoteTargetError(
            "service operations require a session target; create one with "
            "session-management/scripts/session_create.py and pass --session-id"
        )
    cmd = [sys.executable, str(script_map[action])]
    if target.session_file:
        cmd.extend(["--session-file", str(target.session_file)])
    else:
        cmd.extend(["--session-id", target.session_id])
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
        "target": target.to_dict(),
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


def service_logs(target: RemoteTarget, *, lines: int = 120) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    state, path = _load_serving_state_for_target(target)
    if not state:
        return {
            "status": "needs_input",
            "target": target.to_dict(),
            "started_at": started_at,
            "duration_ms": duration_ms(start),
            "error": "no serving state recorded",
            "logs": {"state_path": str(path)},
        }
    stdout_path = state.get("log_stdout")
    stderr_path = state.get("log_stderr")
    chunks: list[str] = []
    if stdout_path:
        chunks.append(f"echo __STDOUT__; tail -n {int(lines)} {shlex.quote(stdout_path)} 2>/dev/null || true")
    if stderr_path:
        chunks.append(f"echo __STDERR__; tail -n {int(lines)} {shlex.quote(stderr_path)} 2>/dev/null || true")
    result = ssh_exec(target.container_endpoint, "\n".join(chunks), timeout=30, check=False)
    return {
        "status": "ok" if result.returncode == 0 else "failed",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "tail": result.stdout,
        "stderr_tail": result.stderr,
        "logs": {
            "state_path": str(path),
            "stdout": stdout_path,
            "stderr": stderr_path,
            "runtime_dir": state.get("runtime_dir"),
        },
    }


def parity_derived_args(target: RemoteTarget, *, force_reinstall: bool = False) -> dict[str, Any]:
    script = ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts" / "parity_sync.py"
    cmd = [sys.executable, str(script), "--print-derived-args"]
    if target.session_file:
        cmd.extend(["--session-file", str(target.session_file)])
    elif target.session_id:
        cmd.extend(["--session-id", target.session_id])
    else:
        cmd.extend(["--machine", target.alias])
    if force_reinstall:
        cmd.append("--force-reinstall")
    rc, payload, stdout, stderr = run_json_command(cmd, relay_stderr=True)
    if rc != 0:
        raise RemoteTargetError(
            f"failed to derive parity args: stdout={tail_text(stdout)} stderr={tail_text(stderr)}"
        )
    return payload


def _parity_plan_manifest(derived: dict[str, Any]) -> dict[str, Any]:
    script = ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts" / "remote_code_parity.py"
    cmd = [
        sys.executable,
        str(script),
        "plan",
        "--workspace-root",
        derived["workspace_root"],
        "--workspace-id",
        derived["workspace_id"],
        "--server-name",
        derived["server_name"],
        "--runtime-root",
        derived["runtime_root"],
        "--container-identity",
        derived["container_identity"],
        "--container-cache-root",
        derived["container_cache_root"],
    ]
    for preserve in derived.get("preserve_path", []):
        cmd.extend(["--preserve-path", preserve])
    rc, payload, stdout, stderr = run_json_command(cmd, relay_stderr=True)
    if rc != 0:
        raise RemoteTargetError(
            f"failed to build parity plan: stdout={tail_text(stdout)} stderr={tail_text(stderr)}"
        )
    return payload


def _repo_install_reasons(repo: dict[str, Any]) -> list[str]:
    relpath = repo.get("relpath", "")
    patterns = VLLM_ASCEND_REINSTALL_PATTERNS if relpath == "vllm-ascend" else VLLM_REINSTALL_PATTERNS
    reasons = []
    for path in repo.get("changed_paths", []):
        if any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
            reasons.append(path)
    return sorted(set(reasons))


def _consent_state(derived: dict[str, Any]) -> dict[str, Any]:
    scripts = ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from install_consent import load_consent_state, resolve_sync_mode  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    repo_root = Path(derived["workspace_root"]).expanduser().resolve()
    state = load_consent_state(repo_root)
    record = (
        state.get("consents", {})
        .get(derived["server_name"], {})
        .get("containers", {})
        .get(derived["container_identity"])
    )
    return {
        "status": "ok",
        "decision": record.get("decision") if isinstance(record, dict) else "unknown",
        "sync_mode": resolve_sync_mode(state, derived["server_name"], derived["container_identity"]),
        "record": record,
    }


def sync_plan(target: RemoteTarget, *, mode: str, force_reinstall: bool = False) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    derived = parity_derived_args(target, force_reinstall=force_reinstall)
    manifest = _parity_plan_manifest(derived)
    consent = _consent_state(derived)
    install_reasons: dict[str, list[str]] = {}
    for repo in manifest.get("repos", []):
        reasons = _repo_install_reasons(repo)
        if reasons:
            install_reasons[repo.get("relpath", ".")] = reasons
    will_install = mode in {"install", "auto"} and (
        force_reinstall or bool(install_reasons) or consent.get("decision") != "allow"
    )
    if mode == "source-only":
        action = "publish source snapshot to container cache only"
        will_materialize = False
        will_install = False
    elif mode == "materialize":
        action = "publish snapshot and materialize runtime source tree without install/rebuild"
        will_materialize = True
        will_install = False
    elif mode == "auto":
        action = (
            "auto-tier: materialize for pure-Python changes, install only when "
            "native/dependency files changed or first install"
        )
        will_materialize = True
    else:
        action = "publish snapshot, materialize runtime source tree, and run install/rebuild when required"
        will_materialize = True
    return {
        "status": "ok",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "mode": mode,
        "action": action,
        "will_materialize": will_materialize,
        "will_install": will_install,
        "install_reasons": {
            "force_reinstall": force_reinstall,
            "changed_paths": install_reasons,
            "consent": consent,
            "note": "source-only and materialize modes never enter install/rebuild",
        },
        "changed_paths": {
            repo.get("relpath", "."): repo.get("changed_paths", [])
            for repo in manifest.get("repos", [])
        },
        "derived": {
            key: derived.get(key)
            for key in (
                "workspace_root",
                "workspace_id",
                "server_name",
                "runtime_root",
                "container_identity",
                "container_cache_root",
                "container_host",
                "container_port",
                "container_user",
            )
        },
        "artifacts": {"manifest": manifest},
        "logs": {},
    }


def sync_apply(
    target: RemoteTarget,
    *,
    mode: str,
    force_reinstall: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    script = ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts" / "parity_sync.py"
    cmd = [sys.executable, str(script)]
    if target.session_file:
        cmd.extend(["--session-file", str(target.session_file)])
    elif target.session_id:
        cmd.extend(["--session-id", target.session_id])
    else:
        cmd.extend(["--machine", target.alias])
    if force_reinstall:
        cmd.append("--force-reinstall")
    if dry_run:
        cmd.append("--dry-run")
    cmd.extend(["--apply-mode", mode])
    rc, payload, stdout, stderr = run_json_command(cmd, relay_stderr=True)
    status = payload.get("status", "failed")
    if rc != 0 and status not in {"blocked", "needs_input", "needs_repair", "timeout", "failed"}:
        status = "failed"
    return {
        "status": status,
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "mode": mode,
        "returncode": rc,
        "result": payload,
        "stdout_tail": tail_text(stdout),
        "stderr_tail": tail_text(stderr),
        "artifacts": {
            "manifest_path": payload.get("manifest_path"),
            "result": payload,
        },
        "logs": {},
    }


def cleanup(
    target: RemoteTarget,
    *,
    dry_run: bool,
    jobs: bool,
    job_ids: Sequence[str] | None = None,
    service: bool,
    session_container: bool,
    leases: bool,
    known_hosts: bool,
    remote_temp: bool,
    force: bool,
) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    actions: list[dict[str, Any]] = []
    status = "ok"
    service_release_ok = False
    if service:
        if dry_run:
            actions.append({"action": "service-stop", "dry_run": True})
            service_release_ok = True
        else:
            service_result = call_service("stop", target, ["--force"] if force else [])
            actions.append({"action": "service-stop", "result": service_result})
            service_release_ok = (
                service_result.get("returncode") == 0
                and service_result.get("status") in {"stopped", "not_found"}
            )
    if jobs or remote_temp:
        remote_paths = []
        if jobs:
            if job_ids:
                remote_paths.extend(
                    str(PurePosixPath(target.remote_toolbox_root()) / "jobs" / require_safe_id(job_id, label="job id"))
                    for job_id in job_ids
                )
            else:
                remote_paths.append(str(PurePosixPath(target.remote_toolbox_root()) / "jobs"))
        if remote_temp:
            remote_paths.append(str(PurePosixPath(target.remote_toolbox_root()) / "tmp"))
        if dry_run:
            actions.append({"action": "remote-rm", "paths": remote_paths, "dry_run": True})
        elif remote_paths:
            script = "rm -rf " + " ".join(shlex.quote(path) for path in remote_paths)
            result = ssh_exec(target.container_endpoint, script, timeout=60, check=False)
            actions.append({
                "action": "remote-rm",
                "paths": remote_paths,
                "returncode": result.returncode,
                "stderr_tail": tail_text(result.stderr),
            })
    if session_container:
        if not target.session_id:
            actions.append({"action": "session-remove", "skipped": True, "reason": "target is not a session"})
        elif dry_run:
            actions.append({"action": "session-remove", "session_id": target.session_id, "dry_run": True})
        else:
            script = ROOT / ".agents" / "skills" / "session-management" / "scripts" / "session_remove.py"
            cmd = [sys.executable, str(script), "--session-file", str(target.session_file), "--remove-container"]
            if leases:
                cmd.append("--release-leases")
            if force:
                cmd.append("--force")
            rc, payload, stdout, stderr = run_json_command(cmd, relay_stderr=True)
            actions.append({
                "action": "session-remove",
                "returncode": rc,
                "result": payload,
                "stdout_tail": tail_text(stdout),
                "stderr_tail": tail_text(stderr),
            })
    elif leases and target.session_id:
        if not service:
            actions.append({
                "action": "release-leases",
                "session_id": target.session_id,
                "blocked": True,
                "reason": "lease release requires --service, --session-container, or --all for session targets",
            })
            status = "blocked"
        elif dry_run:
            actions.append({"action": "release-leases", "session_id": target.session_id, "dry_run": True})
        elif not service_release_ok:
            actions.append({
                "action": "release-leases",
                "session_id": target.session_id,
                "blocked": True,
                "reason": "service stop did not prove resources are safe to release",
            })
            status = "blocked"
        else:
            released = release_all_session_leases(
                session=target.session,
                host_endpoint=target.host_endpoint,
            )
            actions.append({
                "action": "release-leases",
                "session_id": target.session_id,
                "released": released.get("status") == "released",
                "status": released.get("status"),
                "reason": released.get("reason"),
            })
            if released.get("status") != "released":
                status = "blocked"
    if known_hosts:
        for endpoint in (target.host_endpoint, target.container_endpoint):
            key = endpoint.known_hosts_key()
            if dry_run:
                actions.append({"action": "known-hosts-remove", "key": key, "dry_run": True})
            else:
                result = subprocess.run(["ssh-keygen", "-R", key], capture_output=True, text=True, check=False)
                actions.append({
                    "action": "known-hosts-remove",
                    "key": key,
                    "returncode": result.returncode,
                    "stderr_tail": tail_text(result.stderr),
                })
    proof = {
        "target_after_cleanup": target.to_dict(),
        "known_hosts": {
            "host": known_hosts_status(target.host_endpoint),
            "container": known_hosts_status(target.container_endpoint),
        },
    }
    if target.session:
        proof["leases"] = (target.session.get("leases") or {})
    return {
        "status": status,
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "dry_run": dry_run,
        "actions": actions,
        "artifacts": {"proof": proof},
        "logs": {},
    }


def cli_sync_plan(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan remote code sync without mutating runtime.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--mode", choices=("auto", "source-only", "materialize", "install"), default="auto")
    parser.add_argument("--force-reinstall", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        print_json(sync_plan(target_from_args(args), mode=args.mode, force_reinstall=args.force_reinstall))
        return 0
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_sync_apply(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply remote code sync in a selected mode.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--mode", choices=("auto", "source-only", "materialize", "install"), default="auto")
    parser.add_argument("--force-reinstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = sync_apply(
            target_from_args(args),
            mode=args.mode,
            force_reinstall=args.force_reinstall,
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
        payload = call_service("start", target_from_args(args), extra)
        print_json(payload)
        return 0 if payload["status"] == "ready" else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_service_status(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check vLLM service status.", allow_abbrev=False)
    add_target_args(parser)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = call_service("status", target_from_args(args), [])
        print_json(payload)
        return 0 if payload["status"] in {"ready", "alive", "alive_healthy", "stopped", "not_found"} else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_service_logs(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tail vLLM service logs.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--lines", type=int, default=120)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = service_logs(target_from_args(args), lines=args.lines)
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
        payload = call_service("stop", target_from_args(args), ["--force"] if args.force else [])
        print_json(payload)
        return 0 if payload["status"] in {"stopped", "not_found"} else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)


def cli_cleanup(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean remote session/service/temp state.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jobs", action="store_true")
    parser.add_argument("--job-id", action="append", default=[], help="cleanup only this remote job id (repeatable)")
    parser.add_argument("--service", action="store_true")
    parser.add_argument("--session-container", action="store_true")
    parser.add_argument("--leases", action="store_true")
    parser.add_argument("--known-hosts", action="store_true")
    parser.add_argument("--remote-temp", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        for job_id in args.job_id:
            require_safe_id(job_id, label="job id")
        if args.job_id:
            args.jobs = True
        if args.all:
            args.jobs = args.service = args.remote_temp = True
            args.known_hosts = args.known_hosts or False
            if getattr(args, "session_id", None) or getattr(args, "session_file", None):
                args.session_container = True
                args.leases = True
        payload = cleanup(
            target_from_args(args),
            dry_run=args.dry_run,
            jobs=args.jobs,
            job_ids=args.job_id,
            service=args.service,
            session_container=args.session_container,
            leases=args.leases,
            known_hosts=args.known_hosts,
            remote_temp=args.remote_temp,
            force=args.force,
        )
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)
