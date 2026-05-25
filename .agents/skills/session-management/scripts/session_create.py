#!/usr/bin/env python3
"""Create or reuse an isolated VAWS agent session."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
MM_SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"
for _p in (str(LIB_DIR), str(MM_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import inventory as inventory_store  # noqa: E402
import manage_machine as machine_ops  # noqa: E402
from _workflow_common import bootstrap_container, host_target, verify_machine  # noqa: E402
from vaws_session_id import resolve_session_id, write_current_session_binding  # noqa: E402
from vaws_session_state import (  # noqa: E402
    DEFAULT_CONTAINER_SSH_PORT_RANGE,
    SessionStateError,
    allocate_session_leases,
    default_branch,
    default_worktree_root,
    load_session_lookup,
    release_all_session_leases,
    require_session_id,
    save_session,
    session_container_name,
    session_file_path,
    session_record_for_execution,
)
from vaws_local_state import load_profile, utc_now_iso  # noqa: E402
from vaws_validate import ValidationError, parse_device_csv  # noqa: E402

PROGRESS_SENTINEL = "__VAWS_SESSION_PROGRESS__="
PORT_TAIL_RE = re.compile(r"[:.]([0-9]+)$")


def emit_progress(phase: str, message: str, **extra: Any) -> None:
    payload = {"phase": phase, "message": message}
    payload.update({key: value for key, value in extra.items() if value is not None})
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def tail_output(value: str | bytes | None, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-limit:]


def run_git(args: list[str], *, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"git {' '.join(args)} failed")
    return proc


def load_machine(identifier: str) -> dict[str, Any]:
    read_path = inventory_store.read_inventory_path(
        inventory_store.preferred_inventory_path(inventory_store.DEFAULT_PATH)
    )
    inv = inventory_store.load_inventory(read_path)
    matches = inventory_store._find_matches(inv, identifier=identifier)  # noqa: SLF001
    if not matches:
        raise RuntimeError(f"machine {identifier!r} not found in inventory")
    if len(matches) > 1:
        raise RuntimeError(f"machine {identifier!r} matched multiple records")
    return matches[0]


def parse_host_npu_devices(stdout: str) -> list[int]:
    devices: set[int] = set()
    for line in stdout.splitlines():
        match = re.match(r"\|\s*(\d+)\s+\d*\w+\d+\w*\s+\|", line)
        if match:
            devices.add(int(match.group(1)))
    return sorted(devices)


def probe_host_npu_devices(record: dict[str, Any]) -> tuple[list[int] | None, dict[str, Any]]:
    host = record["host"]
    target = machine_ops.SshTarget(
        host=host["ip"],
        user=host.get("user", "root"),
        port=host.get("port", 22),
    )
    cmd = [
        *machine_ops.ssh_command(target),
        "bash",
        "-c",
        shlex.quote("npu-smi info 2>/dev/null"),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except subprocess.TimeoutExpired as exc:
        return None, {
            "status": "timeout",
            "timeout_seconds": 30,
            "stdout_tail": tail_output(exc.stdout),
            "stderr_tail": tail_output(exc.stderr),
        }
    payload: dict[str, Any] = {
        "returncode": result.returncode,
        "stderr_tail": result.stderr[-500:],
    }
    if result.returncode != 0:
        payload["status"] = "unavailable"
        return None, payload
    devices = parse_host_npu_devices(result.stdout)
    payload.update({"status": "ok" if devices else "unparsed", "devices": devices})
    return devices or None, payload


def host_port_available(record: dict[str, Any]) -> Any:
    host = record["host"]

    def check(port: int) -> bool:
        script = f"! ss -ltnH 2>/dev/null | awk '{{print $4}}' | grep -Eq '[:.]({port})$'"
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "LogLevel=ERROR",
            "-p",
            str(host.get("port", 22)),
            f"{host.get('user', 'root')}@{host['ip']}",
            "bash",
            "-c",
            shlex.quote(script),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return result.returncode == 0

    return check


def _parse_listening_ports(stdout: str) -> set[int]:
    ports: set[int] = set()
    for line in stdout.splitlines():
        match = PORT_TAIL_RE.search(line.strip())
        if match:
            ports.add(int(match.group(1)))
    return ports


def host_listening_ports(record: dict[str, Any]) -> set[int] | None:
    host = record["host"]
    script = """
if command -v ss >/dev/null 2>&1; then
  ss -ltnH 2>/dev/null | awk '{print $4}'
elif command -v netstat >/dev/null 2>&1; then
  netstat -ltn 2>/dev/null | awk 'NR > 2 {print $4}'
else
  exit 42
fi
"""
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(host.get("port", 22)),
        f"{host.get('user', 'root')}@{host['ip']}",
        "bash",
        "-c",
        shlex.quote(script),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return _parse_listening_ports(result.stdout)


def host_port_availability(record: dict[str, Any]) -> Any:
    busy_ports = host_listening_ports(record)
    if busy_ports is None:
        return host_port_available(record)
    return lambda port: port not in busy_ports


def verify_session_ssh(
    base_record: dict[str, Any],
    *,
    container_ssh_port: int,
    public_key_file: str | None,
) -> dict[str, Any]:
    """Verify only host/container SSH for a newly bootstrapped session container."""
    try:
        identity_file = machine_ops.private_key_for_public_key(machine_ops.find_public_key(public_key_file))
    except machine_ops.MachineManagementError:
        identity_file = None

    host = machine_ops.SshTarget(
        host=base_record["host"]["ip"],
        user=base_record["host"].get("user", "root"),
        port=base_record["host"].get("port", 22),
    )
    container = machine_ops.SshTarget(
        host=base_record["host"]["ip"],
        user="root",
        port=container_ssh_port,
    )
    host_check = machine_ops.check_direct_ssh(host, identity_file=identity_file)
    container_check = machine_ops.check_direct_ssh(container, identity_file=identity_file)
    payload: dict[str, Any] = {
        "verification_mode": "ssh",
        "identity_file": str(identity_file) if identity_file is not None else None,
        "host_ssh": host_check,
        "container_ssh": container_check,
        "smoke": {
            "success": None,
            "skipped": "session creation defaulted to SSH-only verification; use --verification-mode full for torch/torch_npu smoke",
        },
        "npu_smoke_skipped": True,
    }
    local_tool_errors = []
    for check in (host_check, container_check):
        stderr = check.get("stderr")
        if isinstance(stderr, str) and stderr.startswith("required local command not found:"):
            local_tool_errors.append(stderr)
    if local_tool_errors:
        payload.update(
            {
                "success": False,
                "status": "blocked",
                "action": "missing-local-tool",
                "message": "required local SSH tooling is missing",
                "local_tool_errors": sorted(set(local_tool_errors)),
                "ready": False,
            }
        )
        return payload
    ready = bool(host_check.get("ok") and container_check.get("ok"))
    payload.update(
        {
            "success": ready,
            "status": "ready" if ready else "needs_repair",
            "action": "ssh-verified" if ready else "ssh-verify-found-drift",
            "ready": ready,
        }
    )
    if not ready:
        payload["message"] = "session container SSH is not ready"
    return payload


def existing_worktree_bound(path: Path) -> str | None:
    binding = path / ".vaws-local" / "current-session.json"
    if not binding.exists():
        return None
    try:
        data = json.loads(binding.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    sid = data.get("session_id")
    return str(sid) if sid else None


def ensure_worktree(
    *,
    session_id: str,
    branch: str,
    base_ref: str,
    worktree_root: Path,
    no_worktree: bool,
) -> tuple[Path, dict[str, Any]]:
    if no_worktree:
        return ROOT, {"action": "current-repo", "path": str(ROOT)}

    worktree_root = worktree_root.expanduser().resolve()
    if worktree_root.exists():
        bound = existing_worktree_bound(worktree_root)
        if bound == session_id:
            emit_progress("worktree", "reusing bound worktree", path=str(worktree_root), branch=branch)
            emit_progress("worktree", "initializing submodules", path=str(worktree_root))
            run_git(["submodule", "update", "--init", "--recursive"], cwd=worktree_root)
            return worktree_root, {"action": "reused", "path": str(worktree_root), "branch": branch}
        raise SessionStateError(
            f"worktree path already exists and is not bound to session {session_id}: {worktree_root}"
        )

    emit_progress("worktree", f"creating worktree {worktree_root}", branch=branch)
    branch_exists = run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0
    worktree_root.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists:
        run_git(["worktree", "add", str(worktree_root), branch])
        action = "added-existing-branch"
    else:
        run_git(["worktree", "add", "-b", branch, str(worktree_root), base_ref])
        action = "created-branch"
    staging_binding = write_current_session_binding(
        worktree_root,
        session_id=session_id,
        source="session_create-staging",
        base_repo_root=ROOT,
    )
    emit_progress("worktree", "initializing submodules", path=str(worktree_root))
    run_git(["submodule", "update", "--init", "--recursive"], cwd=worktree_root)
    return worktree_root, {
        "action": action,
        "path": str(worktree_root),
        "branch": branch,
        "base_ref": base_ref,
        "staging_binding": str(staging_binding),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--machine", required=True, help="base machine alias or host IP")
    parser.add_argument("--session-id", help="explicit session id; otherwise resolver fallback is used")
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--branch", help="worktree branch; defaults to session/<id>")
    parser.add_argument("--worktree-root", type=Path, help="override local worktree path")
    parser.add_argument("--no-worktree", action="store_true", help="bind the session to the current repo root")
    parser.add_argument("--image", help="override the base machine image for this session container")
    parser.add_argument("--devices", help="comma-separated NPU device ids to lease")
    parser.add_argument("--npu-count", type=int, help="lease the first N locally unleased devices")
    parser.add_argument("--container-ssh-port", type=int, help="explicit session container SSH port")
    parser.add_argument("--container-ssh-port-range", default=DEFAULT_CONTAINER_SSH_PORT_RANGE)
    parser.add_argument("--runtime-profile", default="vllm-ascend")
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--reuse-existing", action="store_true")
    parser.add_argument("--skip-container-bootstrap", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--disable-prepared-image-cache",
        action="store_true",
        help="do not use the host-local session-ready image cache for this container",
    )
    parser.add_argument(
        "--verification-mode",
        choices=("ssh", "full"),
        default="ssh",
        help="session readiness check after bootstrap; default avoids repeated NPU smoke during parallel session creation",
    )
    parser.add_argument("--replace-container-on-image-change", action="store_true")
    parser.add_argument("--public-key-file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session_path: Path | None = None
    binding_payload: dict[str, Any] | None = None
    try:
        resolved = resolve_session_id(
            explicit=args.session_id,
            repo_root=ROOT,
            persist_generated=False,
            use_current_binding=False,
        )
        sid = require_session_id(resolved.value)

        if args.reuse_existing:
            try:
                existing = load_session_lookup(session_id=sid, repo_root=ROOT)
                print_json(
                    {
                        "status": existing.session.get("status", "ready"),
                        "session_id": sid,
                        "session_file": str(existing.session_file),
                        "worktree_root": existing.session.get("local", {}).get("worktree_root"),
                        "container": existing.session.get("remote", {}).get("container"),
                        "reused": True,
                    }
                )
                return 0
            except SessionStateError as exc:
                if "session id or session file is required" not in str(exc) and "session file does not exist" not in str(exc):
                    print_json({"status": "needs_repair", "session_id": sid, "error": str(exc)})
                    return 1

        emit_progress("resolve-machine", f"loading base machine {args.machine}")
        base_record = load_machine(args.machine)
        alias = base_record["alias"]
        namespace = base_record.get("namespace")
        if not namespace:
            profile = load_profile()
            namespace = profile.get("machine_username") if profile else None

        image = args.image or base_record["container"]["image"]
        workdir = args.workdir or base_record["container"].get("workdir", "/vllm-workspace")
        runtime_root = args.runtime_root or workdir
        branch = args.branch or default_branch(sid)
        worktree_root = args.worktree_root or default_worktree_root(ROOT, sid)
        requested_devices = parse_device_csv(args.devices)
        if requested_devices is not None and args.npu_count is not None:
            raise SessionStateError("use only one of --devices or --npu-count")
        if args.npu_count is not None and args.npu_count < 1:
            raise SessionStateError("--npu-count must be >= 1")
        available_devices, npu_probe = probe_host_npu_devices(base_record)
        if available_devices is None:
            emit_progress("probe-npus", "host NPU device probe unavailable; validating syntax only", **npu_probe)
        else:
            emit_progress("probe-npus", "host NPU device probe succeeded", devices=available_devices)

        local_root, worktree_payload = ensure_worktree(
            session_id=sid,
            branch=branch,
            base_ref=args.base_ref,
            worktree_root=worktree_root,
            no_worktree=args.no_worktree,
        )
        if args.no_worktree and args.session_id:
            binding_payload = {
                "action": "skipped",
                "reason": "explicit --no-worktree session does not overwrite repo-root current-session",
                "path": str(local_root / ".vaws-local" / "current-session.json"),
            }
        elif worktree_payload.get("staging_binding"):
            binding_payload = {
                "action": "written",
                "path": worktree_payload["staging_binding"],
                "source": "session_create-staging",
            }
        else:
            binding_path = write_current_session_binding(
                local_root,
                session_id=sid,
                source="session_create-staging",
                base_repo_root=ROOT,
            )
            binding_payload = {"action": "written", "path": str(binding_path)}

        emit_progress("lease", "allocating session resources", machine=alias)
        leases = allocate_session_leases(
            repo_root=ROOT,
            machine_alias=alias,
            session_id=sid,
            requested_devices=requested_devices,
            npu_count=args.npu_count,
            available_devices=available_devices,
            container_ssh_port=args.container_ssh_port,
            container_ssh_port_range=args.container_ssh_port_range,
            port_available=host_port_availability(base_record),
        )

        container_name = session_container_name(namespace, sid)
        now = utc_now_iso()
        session = {
            "schema_version": 1,
            "session_id": sid,
            "session_id_source": resolved.source,
            "base_machine": alias,
            "workspace_id": sid,
            "status": "creating",
            "local": {
                "worktree_root": str(local_root),
                "base_repo_root": str(ROOT),
                "branch": branch,
                "base_ref": args.base_ref,
            },
            "remote": {
                "host": base_record["host"]["ip"],
                "host_user": base_record["host"].get("user", "root"),
                "host_port": base_record["host"].get("port", 22),
                "namespace": namespace,
                "machine_type": base_record["host"].get("machine_type") or base_record["container"].get("machine_type"),
                "soc": base_record["host"].get("soc"),
                "container": {
                    "name": container_name,
                    "ssh_port": leases["container_ssh_port"],
                    "image": image,
                    "workdir": workdir,
                    "runtime_root": runtime_root,
                    "machine_type": base_record["container"].get("machine_type") or base_record["host"].get("machine_type"),
                },
            },
            "leases": {
                "npu_devices": leases.get("npu_devices", []),
                "container_ssh_port": leases["container_ssh_port"],
                "service_ports": [],
            },
            "runtime_profile": {
                "type": args.runtime_profile,
                "parity": "local",
                "serve_adapter": "vllm-ascend-serving",
                "bench_adapter": "vllm-ascend-benchmark",
            },
            "created_at": now,
            "updated_at": now,
        }
        if npu_probe:
            session["leases"]["npu_probe"] = npu_probe
        session_path = save_session(session, repo_root=ROOT)
        if binding_payload and binding_payload.get("action") == "written":
            binding_path = write_current_session_binding(
                local_root,
                session_id=sid,
                source="session_create",
                session_file=session_path,
                base_repo_root=ROOT,
            )
            binding_payload = {"action": "written", "path": str(binding_path)}

        container_payload: dict[str, Any] = {"status": "skipped"} if args.skip_container_bootstrap else {}
        if not args.skip_container_bootstrap:
            emit_progress("container", f"bootstrapping session container {container_name}", machine=alias)
            target = host_target(
                host=base_record["host"]["ip"],
                user=base_record["host"].get("user", "root"),
                port=base_record["host"].get("port", 22),
            )
            container_payload = bootstrap_container(
                target,
                host=base_record["host"]["ip"],
                container_name=container_name,
                container_ssh_port=leases["container_ssh_port"],
                image=image,
                workdir=workdir,
                namespace=namespace,
                machine_type=base_record["host"].get("machine_type") or base_record["container"].get("machine_type"),
                soc=base_record["host"].get("soc"),
                public_key_file=args.public_key_file,
                replace_container_on_image_change=args.replace_container_on_image_change,
                use_prepared_image_cache=not args.disable_prepared_image_cache,
            )
            if container_payload.get("status") in {"needs_input", "needs_repair", "blocked"}:
                session["status"] = "failed"
                session["failure"] = container_payload
                save_session(session, repo_root=ROOT)
                print_json(
                    {
                        "status": "blocked",
                        "session_id": sid,
                        "session_file": str(session_path),
                        "worktree": worktree_payload,
                        "container": container_payload,
                    }
                )
                return 1
            selected_image = container_payload.get("selected_image") or container_payload.get("image")
            if selected_image:
                session["remote"]["container"]["image"] = selected_image
            if container_payload.get("container_type"):
                session["remote"]["container"]["machine_type"] = container_payload["container_type"]
            record = session_record_for_execution(session)
            if args.verification_mode == "full":
                emit_progress("verify", "running full session verification", session_id=sid)
                verify = verify_machine(record)
                verify["verification_mode"] = "full"
            else:
                emit_progress("verify", "checking session SSH readiness", session_id=sid)
                verify = verify_session_ssh(
                    base_record,
                    container_ssh_port=leases["container_ssh_port"],
                    public_key_file=args.public_key_file,
                )
            container_payload["verify"] = verify
            if verify.get("status") != "ready":
                session["status"] = "blocked" if verify.get("status") == "blocked" else "needs_repair"
                session["verify"] = verify
                save_session(session, repo_root=ROOT)
                print_json(
                    {
                        "status": session["status"],
                        "session_id": sid,
                        "session_file": str(session_path),
                        "worktree": worktree_payload,
                        "current_session_binding": binding_payload,
                        "container": container_payload,
                    }
                )
                return 1
            session["status"] = "ready"
        else:
            session["status"] = "planned"
        save_session(session, repo_root=ROOT)

        print_json(
            {
                "status": session["status"],
                "session_id": sid,
                "session_file": str(session_file_path(sid, ROOT)),
                "worktree_root": str(local_root),
                "container": session["remote"]["container"],
                "leases": session["leases"],
                "worktree": worktree_payload,
                "current_session_binding": binding_payload,
                "container_bootstrap": container_payload,
            }
        )
        return 0
    except ValidationError as exc:
        if "sid" in locals():
            with contextlib.suppress(Exception):
                release_all_session_leases(repo_root=ROOT, session_id=sid)
        print_json({"status": "needs_input", "error": str(exc)})
        return 1
    except Exception as exc:
        if "sid" in locals():
            with contextlib.suppress(Exception):
                release_all_session_leases(repo_root=ROOT, session_id=sid)
            if session_path is not None:
                with contextlib.suppress(Exception):
                    failed_session = load_session_lookup(session_file=session_path, repo_root=ROOT).session
                    failed_session["status"] = "failed"
                    failed_session["failure"] = {"error": str(exc)}
                    save_session(failed_session, repo_root=ROOT)
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
