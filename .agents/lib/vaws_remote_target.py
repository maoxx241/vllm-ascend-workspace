"""Resolve VAWS machines and sessions to host/container endpoints.

This is not SSH transport. Endpoint construction and option knowledge live in
``vaws-remote-dev``. This module only maps inventory and session records to
``host`` / ``port`` / ``user`` plus the Ascend runtime preamble used inside
remote scripts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from vaws_local_state import (
    ROOT,
    WorkspaceStateError,
    resolve_inventory_read_path,
    shared_inventory_path,
    utc_now_iso,
)
from vaws_remote_dev import ASCEND_RUNTIME_ENV_FILE, state_dir
from vaws_session_state import (
    load_session_lookup,
    session_record_for_execution,
    session_serving_state_path,
)
from vaws_validate import ValidationError

DEFAULT_REMOTE_TOOLBOX_ROOT = ".vaws-runtime/remote-toolbox"
PROGRESS_SENTINEL = "__VAWS_REMOTE_TOOLBOX_PROGRESS__="
TAIL_CHARS = 12000


class RemoteTargetError(RuntimeError):
    """Deterministic user-facing target-resolution or wrapper failure."""


@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    user: str = "root"

    def destination(self) -> str:
        return f"{self.user}@{self.host}"

    def known_hosts_key(self) -> str:
        return self.host if self.port == 22 else f"[{self.host}]:{self.port}"

    def to_dict(self, *, plane: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "destination": self.destination(),
            "known_hosts_key": self.known_hosts_key(),
        }
        if plane:
            payload["plane"] = plane
        return payload


@dataclass(frozen=True)
class RemoteTarget:
    mode: str
    alias: str
    target_id: str
    workspace_id: str
    workspace_root: Path
    runtime_root: str
    container_name: str
    container_image: str
    container_endpoint: SshEndpoint
    host_endpoint: SshEndpoint
    state_repo_root: Path
    record: dict[str, Any]
    session_id: str | None = None
    session_file: Path | None = None
    session: dict[str, Any] | None = None
    leased_devices: list[int] | None = None

    def remote_toolbox_root(self) -> str:
        return str(PurePosixPath(self.runtime_root) / DEFAULT_REMOTE_TOOLBOX_ROOT)

    def to_dict(self) -> dict[str, Any]:
        remote_state = state_dir(self.state_repo_root)
        state_paths: dict[str, Any] = {
            "repo_root": str(self.state_repo_root),
            "remote_dev": str(remote_state),
            "logs": str(remote_state / "logs"),
            "jobs": str(remote_state / "jobs"),
            "artifacts": str(remote_state / "artifacts"),
        }
        if self.session_id:
            state_paths["session_file"] = str(self.session_file) if self.session_file else None
            state_paths["serving_state"] = str(
                session_serving_state_path(self.session_id, self.state_repo_root)
            )
        else:
            state_paths["serving_state"] = None
        return {
            "mode": self.mode,
            "alias": self.alias,
            "target_id": self.target_id,
            "session_id": self.session_id,
            "session_file": str(self.session_file) if self.session_file else None,
            "workspace_id": self.workspace_id,
            "workspace_root": str(self.workspace_root),
            "runtime_root": self.runtime_root,
            "remote_toolbox_root": self.remote_toolbox_root(),
            "leased_devices": self.leased_devices or [],
            "host": self.host_endpoint.to_dict(plane="host"),
            "container": {
                "name": self.container_name,
                "image_record": self.container_image,
                **self.container_endpoint.to_dict(plane="container"),
            },
            "state_paths": state_paths,
        }


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)


def print_json(data: dict[str, Any]) -> None:
    print(json_dumps(data))


def emit_progress(
    phase: str,
    message: str | None = None,
    *,
    sentinel: str | None = None,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {"phase": phase, "at": utc_now_iso()}
    if message is not None:
        payload["message"] = message
    payload.update({key: value for key, value in extra.items() if value is not None})
    sys.stderr.write(
        (sentinel or PROGRESS_SENTINEL) + json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    )
    sys.stderr.flush()


def now_iso() -> str:
    return utc_now_iso()


def duration_ms(start_monotonic: float) -> int:
    return int(round((time.monotonic() - start_monotonic) * 1000))


def tail_text(value: str, limit: int = TAIL_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def derive_workspace_id(repo_root: Path) -> str:
    base = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in repo_root.name.lower()).strip(".-")
    digest = hashlib.sha1(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{base or 'workspace'}-{digest}"


def load_inventory(repo_root: Path = ROOT) -> tuple[dict[str, Any], Path]:
    preferred = shared_inventory_path(repo_root)
    path = resolve_inventory_read_path(preferred, repo_root=repo_root)
    if not path.exists():
        raise RemoteTargetError(
            f"machine inventory not found at {preferred}; register the machine first "
            "with machine-management/scripts/machine_add.py"
        )
    return json.loads(path.read_text(encoding="utf-8")), path


def _find_machine_record(identifier: str, repo_root: Path = ROOT) -> tuple[dict[str, Any], Path]:
    inventory, path = load_inventory(repo_root)
    matches: list[dict[str, Any]] = []
    for record in inventory.get("machines", []):
        host = record.get("host", {})
        alias = record.get("alias")
        ip = host.get("ip") if isinstance(host, dict) else host
        if identifier in {alias, ip}:
            matches.append(record)
    if not matches:
        raise RemoteTargetError(f"machine {identifier!r} not found in inventory {path}")
    if len(matches) > 1:
        raise RemoteTargetError(f"machine {identifier!r} matched multiple inventory records")
    return matches[0], path


def _container_endpoint(record: dict[str, Any]) -> SshEndpoint:
    host = record.get("host", {})
    container = record.get("container", {})
    if not isinstance(host, dict) or not isinstance(container, dict):
        raise RemoteTargetError("machine record must contain host and container objects")
    port = container.get("ssh_port")
    if not isinstance(port, int):
        raise RemoteTargetError("machine record is missing container.ssh_port")
    return SshEndpoint(host=str(host["ip"]), port=port, user=str(container.get("user", "root")))


def container_endpoint_from_record(record: dict[str, Any]) -> SshEndpoint:
    """Lenient container-SSH endpoint from a machine/session record dict."""
    host_info = record.get("host", {})
    container_info = record.get("container", {})
    if isinstance(host_info, dict):
        ip = str(host_info.get("ip", ""))
        host_port = int(host_info.get("port", 22))
        host_user = str(host_info.get("user", "root"))
    else:
        ip = str(host_info)
        host_port = 22
        host_user = "root"
    if not isinstance(container_info, dict):
        container_info = {}
    ssh_port = container_info.get("ssh_port")
    if ssh_port is not None:
        return SshEndpoint(
            host=ip, port=int(ssh_port), user=str(container_info.get("user") or "root")
        )
    return SshEndpoint(host=ip, port=host_port, user=host_user)


def _host_endpoint(record: dict[str, Any]) -> SshEndpoint:
    host = record.get("host", {})
    if not isinstance(host, dict):
        raise RemoteTargetError("machine record must contain host object")
    return SshEndpoint(
        host=str(host["ip"]),
        port=int(host.get("port", host.get("ssh_port", 22))),
        user=str(host.get("user", "root")),
    )


def resolve_remote_target(
    *,
    machine: str | None = None,
    session_id: str | None = None,
    session_file: str | Path | None = None,
    repo_root: Path = ROOT,
) -> RemoteTarget:
    repo_root = repo_root.expanduser().resolve()
    if machine and (session_id or session_file):
        raise RemoteTargetError("use exactly one target surface: --machine or --session-id/--session-file")
    if not machine:
        lookup = load_session_lookup(
            session_id=session_id,
            session_file=session_file,
            repo_root=repo_root,
        )
        session = lookup.session
        record = session_record_for_execution(session)
        container = record["container"]
        session_container = session["remote"]["container"]
        runtime_root = (
            session_container.get("runtime_root")
            or container.get("workdir")
            or "/vllm-workspace"
        )
        workspace_root = Path(session["local"]["worktree_root"]).expanduser().resolve()
        return RemoteTarget(
            mode="session",
            alias=record["alias"],
            target_id=session["session_id"],
            workspace_id=str(session.get("workspace_id") or session["session_id"]),
            workspace_root=workspace_root,
            runtime_root=runtime_root,
            container_name=str(container.get("name") or session_container["name"]),
            container_image=str(container.get("image") or session_container.get("image") or ""),
            container_endpoint=_container_endpoint(record),
            host_endpoint=_host_endpoint(record),
            state_repo_root=lookup.state_repo_root,
            record=record,
            session_id=session["session_id"],
            session_file=lookup.session_file,
            session=session,
            leased_devices=[int(item) for item in session.get("leases", {}).get("npu_devices", [])],
        )

    record, _ = _find_machine_record(machine, repo_root)
    container = record["container"]
    runtime_root = container.get("runtime_root") or container.get("workdir") or "/vllm-workspace"
    alias = str(record.get("alias") or machine)
    return RemoteTarget(
        mode="legacy",
        alias=alias,
        target_id=alias,
        workspace_id=derive_workspace_id(repo_root),
        workspace_root=repo_root,
        runtime_root=str(runtime_root),
        container_name=str(container.get("name") or ""),
        container_image=str(container.get("image") or ""),
        container_endpoint=_container_endpoint(record),
        host_endpoint=_host_endpoint(record),
        state_repo_root=repo_root,
        record=record,
        leased_devices=[],
    )


def ascend_env_preamble(*, set_e: bool = True, export_driver_lib: bool = False) -> str:
    """Standard Ascend environment preamble for remote bash snippets.

    remote-dev sources the same file through ``Endpoint.runtime_env_file``.
    This helper remains for scripts that embed the preamble inside a larger
    snippet rather than using the endpoint runtime-env flag.
    """
    lines: list[str] = []
    if set_e:
        lines.append("set -e")
    lines.extend(
        [
            f"if [ -f {ASCEND_RUNTIME_ENV_FILE} ]; then",
            "  set +u",
            f"  source {ASCEND_RUNTIME_ENV_FILE}",
            "  set -u",
            "fi",
        ]
    )
    if export_driver_lib:
        lines.append(
            "export LD_LIBRARY_PATH="
            '"/usr/local/Ascend/driver/lib64/driver'
            ":/usr/local/Ascend/driver/lib64"
            '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'
        )
    return "\n".join(lines)


def add_target_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("target")
    group.add_argument("--machine", help="machine alias or host IP")
    group.add_argument("--session-id", help="VAWS session id")
    group.add_argument("--session-file", help="explicit session.json path")


def target_from_args(args: argparse.Namespace) -> RemoteTarget:
    return resolve_remote_target(
        machine=getattr(args, "machine", None),
        session_id=getattr(args, "session_id", None),
        session_file=getattr(args, "session_file", None),
    )


def cli_error(exc: BaseException, *, started_at: str, start: float) -> int:
    status = "failed"
    if isinstance(exc, (RemoteTargetError, WorkspaceStateError, ValidationError, FileNotFoundError)):
        status = "needs_input"
    if isinstance(exc, subprocess.TimeoutExpired):
        status = "timeout"
    print_json({
        "status": status,
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "error": str(exc),
        "target": None,
        "logs": {},
    })
    return 2 if status == "failed" else 1


def run_json_command(cmd: list[str], *, cwd: Path = ROOT, relay_stderr: bool = True) -> tuple[int, dict[str, Any], str, str]:
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if relay_stderr and stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
    try:
        payload = json.loads(stdout) if stdout.strip() else {}
        if not isinstance(payload, dict):
            payload = {"status": "failed", "error": "subcommand returned non-object JSON", "stdout_tail": tail_text(stdout)}
    except json.JSONDecodeError:
        payload = {"status": "failed", "error": "subcommand returned non-JSON stdout", "stdout_tail": tail_text(stdout)}
    return result.returncode, payload, stdout, stderr


def selector_args(args: argparse.Namespace) -> list[str]:
    """Translate toolbox target flags into remote-dev ``--selector`` argv."""
    translated: list[str] = []
    if getattr(args, "machine", None):
        translated.extend(["--selector", f"machine={args.machine}"])
    if getattr(args, "session_id", None):
        translated.extend(["--selector", f"session_id={args.session_id}"])
    if getattr(args, "session_file", None):
        translated.extend(["--selector", f"session_file={args.session_file}"])
    return translated


def cli_target_resolve(argv: Sequence[str] | None = None) -> int:
    started_at = now_iso()
    start = time.monotonic()
    parser = argparse.ArgumentParser(description="Resolve a VAWS remote target.", allow_abbrev=False)
    add_target_args(parser)
    args = parser.parse_args(argv)
    try:
        target = target_from_args(args)
        print_json({
            "status": "ok",
            "target": target.to_dict(),
            "started_at": started_at,
            "duration_ms": duration_ms(start),
            "logs": {},
        })
        return 0
    except Exception as exc:  # noqa: BLE001
        return cli_error(exc, started_at=started_at, start=start)
