"""Ordinary host/port endpoints. Not a VAWS identity resolver."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from vaws_local_state import ROOT, utc_now_iso
from vaws_result_envelope import PROGRESS_SENTINEL, progress as envelope_progress, unwrap_skill_payload
from vaws_validate import ValidationError

TAIL_CHARS = 12000
OPTIONAL_ASCEND_ENV_FILE = "/etc/profile.d/vaws-ascend-env.sh"


class RemoteTargetError(RuntimeError):
    """Deterministic user-facing endpoint failure."""


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


def ssh_endpoint_from_mapping(data: dict[str, Any] | None) -> SshEndpoint:
    if not isinstance(data, dict) or not data.get("host"):
        raise RemoteTargetError("endpoint mapping is missing host")
    return SshEndpoint(
        host=str(data["host"]),
        port=int(data.get("port") or 22),
        user=str(data.get("user") or "root"),
    )


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)


def print_json(data: dict[str, Any]) -> None:
    print(json_dumps(data))


def emit_progress(phase: str, message: str | None = None, *, sentinel: str | None = None, **extra: Any) -> None:
    if sentinel is not None and sentinel != PROGRESS_SENTINEL:
        raise ValueError("progress sentinel is owned by vaws_result_envelope")
    envelope_progress(phase, message or phase, **extra)


def now_iso() -> str:
    return utc_now_iso()


def duration_ms(start_monotonic: float) -> int:
    return int(round((time.monotonic() - start_monotonic) * 1000))


def tail_text(value: str, limit: int = TAIL_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def ascend_env_preamble(*, set_e: bool = True, export_driver_lib: bool = False) -> str:
    """Optional remote snippet. Coordinator launch env is authoritative."""
    lines: list[str] = []
    if set_e:
        lines.append("set -e")
    lines.extend(
        [
            f"if [ -f {OPTIONAL_ASCEND_ENV_FILE} ]; then",
            "  set +u",
            f"  source {OPTIONAL_ASCEND_ENV_FILE}",
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


def cli_error(exc: BaseException, *, started_at: str, start: float) -> int:
    status = "failed"
    if isinstance(exc, (RemoteTargetError, ValidationError, FileNotFoundError)):
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
        else:
            payload = unwrap_skill_payload(payload)
    except json.JSONDecodeError:
        payload = {"status": "failed", "error": "subcommand returned non-JSON stdout", "stdout_tail": tail_text(stdout)}
    return result.returncode, payload, stdout, stderr


def add_target_args(parser: argparse.ArgumentParser) -> None:
    from vaws_task_target import add_task_args

    group = parser.add_argument_group("target")
    group.add_argument("--host", help="explicit remote host")
    group.add_argument("--port", type=int, help="explicit remote SSH port")
    group.add_argument("--user", default="root")
    add_task_args(group)


def selector_args(args: argparse.Namespace) -> list[str]:
    """Ordinary remote-dev endpoint flags. No VAWS resolver selectors."""
    out: list[str] = []
    if getattr(args, "host", None):
        out.extend(["--host", str(args.host)])
        if getattr(args, "port", None):
            out.extend(["--port", str(args.port)])
        if getattr(args, "user", None):
            out.extend(["--user", str(args.user)])
    return out


def endpoint_from_args(args: argparse.Namespace) -> SshEndpoint:
    if getattr(args, "host", None):
        return SshEndpoint(str(args.host), int(getattr(args, "port", None) or 22), str(getattr(args, "user", None) or "root"))
    execution_id = getattr(args, "execution_id", None)
    if not execution_id:
        raise RemoteTargetError("pass --host/--port or --execution-id with task context")
    from vaws_task_target import task_client

    observation = task_client(getattr(args, "context_file", None)).observe(str(execution_id), "status")
    target = observation.get("target") if isinstance(observation.get("target"), dict) else {}
    return ssh_endpoint_from_mapping(target.get("endpoint") or observation.get("endpoint"))
