#!/usr/bin/env python3
"""Agent-facing remote toolbox primitives for VAWS managed containers.

This module is intentionally script-friendly: callers get structured target
resolution, SSH execution with local logs, job state, artifact streaming, and
thin adapters around existing parity/serving/session entrypoints.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tarfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Sequence

from vaws_local_state import (  # noqa: E402
    INVENTORY_PATH,
    LEGACY_INVENTORY_PATH,
    ROOT,
    WorkspaceStateError,
    ensure_state_dir,
    resolve_inventory_read_path,
    utc_now_iso,
)
from vaws_session_state import (  # noqa: E402
    load_leases,
    load_session_lookup,
    release_all_session_leases,
    session_record_for_execution,
    session_serving_state_path,
)
from vaws_validate import (  # noqa: E402
    ValidationError,
    ensure_child_path,
    require_env_name,
    require_remote_leaf,
    require_safe_id,
)


PROGRESS_SENTINEL = "__VAWS_REMOTE_TOOLBOX_PROGRESS__="
STATE_DIR = ROOT / ".vaws-local" / "remote-toolbox"
LOG_DIR = STATE_DIR / "logs"
JOB_STATE_DIR = STATE_DIR / "jobs"
ARTIFACT_STATE_DIR = STATE_DIR / "artifacts"
DEFAULT_CONTAINER_CACHE_ROOT = "/root/.cache/vaws/remote-code-parity"
DEFAULT_REMOTE_TOOLBOX_ROOT = ".vaws-runtime/remote-toolbox"
TAIL_CHARS = 12000

REMOTE_STATUS_VALUES = {
    "ok",
    "ready",
    "needs_input",
    "blocked",
    "failed",
    "timeout",
    "needs_repair",
    "cancelled",
}

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


class RemoteToolboxError(RuntimeError):
    """Deterministic user-facing remote-toolbox failure."""


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
        state_paths: dict[str, Any] = {
            "repo_root": str(self.state_repo_root),
            "remote_toolbox": str(STATE_DIR),
            "logs": str(LOG_DIR),
            "jobs": str(JOB_STATE_DIR),
            "artifacts": str(ARTIFACT_STATE_DIR),
        }
        if self.session_id:
            state_paths["session_file"] = str(self.session_file) if self.session_file else None
            state_paths["serving_state"] = str(
                session_serving_state_path(self.session_id, self.state_repo_root)
            )
        else:
            state_paths["serving_state"] = str(
                self.state_repo_root / ".vaws-local" / "serving" / f"{self.alias}.json"
            )
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


def emit_progress(phase: str, message: str | None = None, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "phase": phase,
        "at": utc_now_iso(),
    }
    if message is not None:
        payload["message"] = message
    payload.update({key: value for key, value in extra.items() if value is not None})
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


def now_iso() -> str:
    return utc_now_iso()


def duration_ms(start_monotonic: float) -> int:
    return int(round((time.monotonic() - start_monotonic) * 1000))


def tail_text(value: str, limit: int = TAIL_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _write_text(path: Path, value: str) -> None:
    ensure_state_dir(path.parent)
    path.write_text(value, encoding="utf-8")


def _atomic_write_json(path: Path, data: Any) -> None:
    ensure_state_dir(path.parent)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(json_dumps(data) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _new_log_dir(kind: str, token: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = token or uuid.uuid4().hex[:8]
    path = LOG_DIR / kind / f"{stamp}-{suffix}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def derive_workspace_id(repo_root: Path) -> str:
    base = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in repo_root.name.lower()).strip(".-")
    digest = hashlib.sha1(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{base or 'workspace'}-{digest}"


def _load_inventory(repo_root: Path = ROOT) -> tuple[dict[str, Any], Path]:
    preferred = repo_root / ".vaws-local" / "machine-inventory.json"
    path = resolve_inventory_read_path(preferred)
    if not path.exists():
        legacy = repo_root / ".machine-inventory.json"
        if legacy.exists():
            path = legacy
        elif INVENTORY_PATH.exists():
            path = INVENTORY_PATH
        elif LEGACY_INVENTORY_PATH.exists():
            path = LEGACY_INVENTORY_PATH
        else:
            return {"schema_version": 1, "machines": []}, path
    return _load_json(path), path


def _find_machine_record(identifier: str, repo_root: Path = ROOT) -> tuple[dict[str, Any], Path]:
    inventory, path = _load_inventory(repo_root)
    matches: list[dict[str, Any]] = []
    for record in inventory.get("machines", []):
        host = record.get("host", {})
        alias = record.get("alias")
        ip = host.get("ip") if isinstance(host, dict) else host
        if identifier in {alias, ip}:
            matches.append(record)
    if not matches:
        raise RemoteToolboxError(f"machine {identifier!r} not found in inventory {path}")
    if len(matches) > 1:
        raise RemoteToolboxError(f"machine {identifier!r} matched multiple inventory records")
    return matches[0], path


def _container_endpoint(record: dict[str, Any]) -> SshEndpoint:
    host = record.get("host", {})
    container = record.get("container", {})
    if not isinstance(host, dict) or not isinstance(container, dict):
        raise RemoteToolboxError("machine record must contain host and container objects")
    port = container.get("ssh_port")
    if not isinstance(port, int):
        raise RemoteToolboxError("machine record is missing container.ssh_port")
    return SshEndpoint(host=str(host["ip"]), port=port, user=str(container.get("user", "root")))


def _host_endpoint(record: dict[str, Any]) -> SshEndpoint:
    host = record.get("host", {})
    if not isinstance(host, dict):
        raise RemoteToolboxError("machine record must contain host object")
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
        raise RemoteToolboxError("use exactly one target surface: --machine or --session-id/--session-file")
    if session_id or session_file:
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

    if not machine:
        raise RemoteToolboxError("--machine is required unless --session-id or --session-file is used")
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


def _ssh_base_cmd(endpoint: SshEndpoint) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(endpoint.port),
        endpoint.destination(),
    ]


def ssh_exec_raw(
    endpoint: SshEndpoint,
    script: str,
    *,
    timeout: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = [*_ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(script)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise RemoteToolboxError(
            f"remote command failed (rc={result.returncode}): {tail_text(result.stderr, 2000)}"
        )
    return result


def ssh_exec_bytes(
    endpoint: SshEndpoint,
    remote_command: str,
    *,
    stdin: BinaryIO | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    cmd = [*_ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(remote_command)]
    return subprocess.run(
        cmd,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _remote_env_exports(env: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for key, value in sorted(env.items()):
        name = require_env_name(key)
        lines.append(f"export {name}={shlex.quote(str(value))}")
    return lines


def _runtime_env_lines(enabled: bool) -> list[str]:
    if not enabled:
        return []
    return [
        "if [ -f /etc/profile.d/vaws-ascend-env.sh ]; then set +u; . /etc/profile.d/vaws-ascend-env.sh; fi",
    ]


def _probe_effective_environment(
    target: RemoteTarget,
    *,
    cwd: str,
    env: dict[str, str],
    runtime_env: bool,
) -> dict[str, Any]:
    script = "\n".join(
        [
            "set +e",
            *_runtime_env_lines(runtime_env),
            *_remote_env_exports(env),
            f"cd {shlex.quote(cwd)} 2>/dev/null || true",
            "PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)",
            "if [ -z \"$PYTHON\" ]; then printf '%s\\n' '{\"status\":\"ok\",\"python\":{\"available\":false}}'; exit 0; fi",
            "\"$PYTHON\" - <<'PY'",
            "import json, os, sys",
            "print(json.dumps({",
            "  'status': 'ok',",
            "  'cwd': os.getcwd(),",
            "  'python': {'available': True, 'executable': sys.executable, 'version': sys.version.split()[0]},",
            "  'ascend_home': os.environ.get('ASCEND_HOME_PATH'),",
            "}, sort_keys=True))",
            "PY",
        ]
    )
    try:
        payload = _remote_json(target.container_endpoint, script, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    return payload


def remote_exec(
    target: RemoteTarget,
    *,
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    runtime_env: bool = True,
    log_kind: str = "exec",
) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    log_dir = _new_log_dir(log_kind)
    cwd = cwd or target.runtime_root
    env = env or {}
    script_lines = [
        "set -o pipefail",
        *_runtime_env_lines(runtime_env),
        f"cd {shlex.quote(cwd)}",
        *_remote_env_exports(env),
        f"bash -c {shlex.quote(command)}",
    ]
    script = "\n".join(script_lines)
    stdout = ""
    stderr = ""
    exit_code: int | None = None
    status = "failed"
    timed_out = False
    try:
        result = ssh_exec_raw(target.container_endpoint, script, timeout=timeout, check=False)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        exit_code = result.returncode
        status = "ok" if result.returncode == 0 else "failed"
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _decode_timeout_stream(exc.stdout)
        stderr = _decode_timeout_stream(exc.stderr)
        status = "timeout"
    effective_environment = _probe_effective_environment(
        target,
        cwd=cwd,
        env=env,
        runtime_env=runtime_env,
    )
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    meta_path = log_dir / "meta.json"
    _write_text(stdout_path, stdout)
    _write_text(stderr_path, stderr)
    payload = {
        "status": status,
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "error": "remote command timed out" if timed_out else (f"remote command exited with code {exit_code}" if exit_code not in (0, None) else None),
        "command": command,
        "cwd": cwd,
        "environment": {
            "runtime_env": runtime_env,
            "env_keys": sorted(env),
            "timeout_seconds": timeout,
            "effective": effective_environment,
        },
        "stdout_tail": tail_text(stdout),
        "stderr_tail": tail_text(stderr),
        "logs": {
            "dir": str(log_dir),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "meta": str(meta_path),
        },
    }
    _atomic_write_json(meta_path, payload)
    return payload


def _decode_timeout_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def known_hosts_status(endpoint: SshEndpoint) -> dict[str, Any]:
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


def _remote_json(endpoint: SshEndpoint, script: str, *, timeout: float | None = 60) -> dict[str, Any]:
    try:
        result = ssh_exec_raw(endpoint, script, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "error": f"remote command timed out after {timeout}s",
            "stdout_tail": tail_text(_decode_timeout_stream(exc.stdout), 2000),
            "stderr_tail": tail_text(_decode_timeout_stream(exc.stderr), 2000),
        }
    if result.returncode != 0:
        return {
            "status": "failed",
            "exit_code": result.returncode,
            "stdout_tail": tail_text(result.stdout or "", 2000),
            "stderr_tail": tail_text(result.stderr or "", 2000),
        }
    text = result.stdout.strip()
    if not text:
        return {"status": "failed", "error": "remote command returned empty stdout"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "status": "failed",
            "error": f"remote command returned non-JSON: {exc}",
            "stdout_tail": tail_text(result.stdout or "", 2000),
            "stderr_tail": tail_text(result.stderr or "", 2000),
        }
    if isinstance(data, dict):
        return data
    return {"status": "failed", "error": "remote JSON was not an object", "value": data}


def probe_remote(target: RemoteTarget, *, timeout: float | None = 90) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    emit_progress("probe-host", "checking host/container image facts", target=target.target_id)
    host_script = f"""
set +e
if command -v docker >/dev/null 2>&1; then
  docker inspect {shlex.quote(target.container_name)} 2>/dev/null | python3 -c '
import json,sys
try:
    data=json.load(sys.stdin)[0]
    print(json.dumps({{
      "status":"ok",
      "container": {{
        "name": data.get("Name","").lstrip("/"),
        "config_image": data.get("Config",{{}}).get("Image"),
        "image_id": data.get("Image"),
        "state": data.get("State",{{}}),
        "network_settings": {{"ip_address": data.get("NetworkSettings",{{}}).get("IPAddress")}},
      }}
    }}))
except Exception as exc:
    print(json.dumps({{"status":"failed","error":str(exc)}}))
'
else
  printf '%s\\n' '{{"status":"failed","error":"docker not found on host"}}'
fi
"""
    host_facts = _remote_json(target.host_endpoint, host_script, timeout=timeout)

    emit_progress("probe-container", "checking runtime inside container", target=target.target_id)
    runtime_path = json.dumps(target.runtime_root)
    container_script = f"""
set +e
if [ -f /etc/profile.d/vaws-ascend-env.sh ]; then set +u; . /etc/profile.d/vaws-ascend-env.sh; set -u; fi
PYTHON=""
for candidate in /usr/local/python3.11.15/bin/python3 /usr/local/python3.11.14/bin/python3 /usr/local/python3.10/bin/python3 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
done
if [ -z "$PYTHON" ]; then
  printf '%s\\n' '{{"status":"failed","python":{{"available":false}},"error":"python not found"}}'
  exit 0
fi
"$PYTHON" - <<'PY'
import glob
import importlib
import json
import os
import pathlib
import platform
import subprocess
import sys

runtime_root = {runtime_path}

def run(cmd, timeout=12):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return {{"returncode": p.returncode, "stdout": p.stdout[-4000:], "stderr": p.stderr[-2000:]}}
    except Exception as exc:
        return {{"returncode": None, "error": str(exc)}}

def module_info(name):
    try:
        mod = importlib.import_module(name)
        return {{
            "available": True,
            "version": str(getattr(mod, "__version__", "unknown")),
            "file": str(getattr(mod, "__file__", "")),
        }}
    except BaseException as exc:
        return {{"available": False, "error": repr(exc)}}

def read_first(paths):
    for path in paths:
        try:
            p = pathlib.Path(path)
            if p.is_file():
                return {{"path": str(p), "content": p.read_text(errors="replace")[:2000]}}
        except Exception:
            pass
    return None

version_paths = []
version_paths.extend(glob.glob("/usr/local/Ascend/ascend-toolkit/*/version.info"))
version_paths.extend(glob.glob("/usr/local/Ascend/ascend-toolkit/latest/*version*"))
version_paths.extend(glob.glob("/usr/local/Ascend/cann-*/*version*"))
version_paths.extend(glob.glob("/usr/local/Ascend/cann-*/*/version.info"))
version_paths.extend(glob.glob("/usr/local/Ascend/cann/*/version.info"))
try:
    cann_home_real = str(pathlib.Path("/usr/local/Ascend/cann").resolve())
except Exception:
    cann_home_real = None

workspace = {{"runtime_root": runtime_root, "exists": pathlib.Path(runtime_root).exists()}}
for rel in ["", "vllm", "vllm-ascend"]:
    repo = pathlib.Path(runtime_root) / rel if rel else pathlib.Path(runtime_root)
    if repo.exists():
        workspace[rel or "."] = run(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=5)

payload = {{
    "status": "ok",
    "hostname": platform.node(),
    "python": {{
        "executable": sys.executable,
        "version": sys.version.split()[0],
        "version_full": sys.version,
    }},
    "modules": {{
        name: module_info(name) for name in ["torch", "torch_npu", "vllm", "vllm_ascend"]
    }},
    "cann": {{
        "ascend_home": os.environ.get("ASCEND_HOME_PATH"),
        "cann_home": cann_home_real,
        "version_info": read_first(version_paths),
    }},
    "npu": {{
        "npu_smi": run(["npu-smi", "info"], timeout=15),
    }},
    "workspace": workspace,
    "os_release": read_first(["/etc/os-release"]),
}}
print(json.dumps(payload, sort_keys=True))
PY
"""
    container_facts = _remote_json(target.container_endpoint, container_script, timeout=timeout)
    service_facts = probe_service_state(target)
    return {
        "status": "ok" if container_facts.get("status") == "ok" else "needs_repair",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "facts": {
            "image": {
                "recorded_tag": target.container_image,
                "host_docker_inspect": host_facts,
                "note": "recorded tag is untrusted; host_docker_inspect.image_id/config_image are observed from the running container",
            },
            "container_runtime": container_facts,
            "service": service_facts,
            "known_hosts": {
                "host": known_hosts_status(target.host_endpoint),
                "container": known_hosts_status(target.container_endpoint),
            },
        },
        "logs": {},
    }


def _load_serving_state_for_target(target: RemoteTarget) -> tuple[dict[str, Any] | None, Path]:
    if target.session_id:
        path = session_serving_state_path(target.session_id, target.state_repo_root)
    else:
        path = target.state_repo_root / ".vaws-local" / "serving" / f"{target.alias}.json"
    if not path.exists():
        return None, path
    try:
        return _load_json(path), path
    except Exception:
        return None, path


def probe_service_state(target: RemoteTarget) -> dict[str, Any]:
    state, path = _load_serving_state_for_target(target)
    payload: dict[str, Any] = {"state_path": str(path), "recorded": state is not None}
    if not state:
        return payload
    payload["state"] = {k: state.get(k) for k in ("status", "pid", "port", "base_url", "model", "runtime_dir", "log_stdout", "log_stderr")}
    pid = state.get("pid")
    port = state.get("port")
    if pid:
        alive = ssh_exec_raw(target.container_endpoint, f"kill -0 {int(pid)} 2>/dev/null && echo alive || echo dead", check=False)
        payload["pid_alive"] = (alive.stdout or "").strip() == "alive"
    if port:
        health = ssh_exec_raw(
            target.container_endpoint,
            f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 2 http://127.0.0.1:{int(port)}/health 2>/dev/null || echo 000",
            check=False,
        )
        payload["health_code"] = (health.stdout or "").strip()
    return payload


def _parse_env_items(items: Sequence[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise RemoteToolboxError(f"bad env item {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        env[require_env_name(key)] = value
    return env


def _remote_job_dir(target: RemoteTarget, job_id: str) -> str:
    safe_job_id = require_remote_leaf(job_id, label="job id")
    return str(PurePosixPath(target.remote_toolbox_root()) / "jobs" / safe_job_id)


def _job_record_path(job_id: str) -> Path:
    safe_job_id = require_safe_id(job_id, label="job id")
    return ensure_child_path(JOB_STATE_DIR, JOB_STATE_DIR / f"{safe_job_id}.json")


def _save_job_record(job_id: str, record: dict[str, Any]) -> None:
    _atomic_write_json(_job_record_path(job_id), record)


def _load_job_record(job_id: str) -> dict[str, Any]:
    path = _job_record_path(job_id)
    if not path.exists():
        raise RemoteToolboxError(f"unknown remote job id: {job_id}")
    return _load_json(path)


def start_remote_job(
    target: RemoteTarget,
    *,
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    kind: str = "command",
    timeout_seconds: int | None = None,
    job_id: str | None = None,
    runtime_env: bool = True,
) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    job_id = require_safe_id(
        job_id or f"job-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        label="job id",
    )
    remote_dir = _remote_job_dir(target, job_id)
    cwd = cwd or target.runtime_root
    env = env or {}
    status_json = json.dumps({"status": "running", "job_id": job_id, "started_at": started_at})
    meta = {
        "schema_version": 1,
        "job_id": job_id,
        "kind": kind,
        "target": target.to_dict(),
        "command": command,
        "cwd": cwd,
        "env_keys": sorted(env),
        "runtime_env": runtime_env,
        "remote_dir": remote_dir,
        "started_at": started_at,
        "timeout_seconds": timeout_seconds,
    }
    env_lines = _remote_env_exports(env)
    timeout_prefix = f"timeout {int(timeout_seconds)} " if timeout_seconds else ""
    runner = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set +e",
            f"JOB_DIR={shlex.quote(remote_dir)}",
            *_runtime_env_lines(runtime_env),
            f"cd {shlex.quote(cwd)}",
            *env_lines,
            f"printf '%s\\n' {shlex.quote(status_json)} > \"$JOB_DIR/status.json\"",
            "date -u +%Y-%m-%dT%H:%M:%SZ > \"$JOB_DIR/started_at\"",
            f"{timeout_prefix}bash -c {shlex.quote(command)} > \"$JOB_DIR/stdout.log\" 2> \"$JOB_DIR/stderr.log\"",
            "rc=$?",
            "finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)",
            "status=failed",
            "[ \"$rc\" -eq 0 ] && status=succeeded",
            "if [ \"$rc\" -eq 124 ] || [ \"$rc\" -eq 137 ]; then status=timeout; fi",
            "cat > \"$JOB_DIR/status.json\" <<EOF",
            '{"status":"'"$status"'","job_id":"'
            + job_id
            + '","exit_code":'"$rc"',"finished_at":"'"$finished"'"}',
            "EOF",
            "exit 0",
        ]
    )
    remote_meta = json_dumps(meta)
    script = f"""
set -e
mkdir -p {shlex.quote(remote_dir)}
cat > {shlex.quote(str(PurePosixPath(remote_dir) / "meta.json"))} <<'VAWS_META'
{remote_meta}
VAWS_META
cat > {shlex.quote(str(PurePosixPath(remote_dir) / "run.sh"))} <<'VAWS_RUN'
{runner}
VAWS_RUN
chmod +x {shlex.quote(str(PurePosixPath(remote_dir) / "run.sh"))}
nohup bash {shlex.quote(str(PurePosixPath(remote_dir) / "run.sh"))} >/dev/null 2>&1 </dev/null &
pid=$!
echo "$pid" > {shlex.quote(str(PurePosixPath(remote_dir) / "pid"))}
printf '{{"pid":%s}}\\n' "$pid"
"""
    result = _remote_json(target.container_endpoint, script, timeout=20)
    if "pid" not in result:
        return {
            "status": "failed",
            "target": target.to_dict(),
            "started_at": started_at,
            "duration_ms": duration_ms(start),
            "error": "failed to launch remote job",
            "result": result,
            "logs": {},
        }
    meta["pid"] = result["pid"]
    _save_job_record(job_id, meta)
    return {
        "status": "ok",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "job_id": job_id,
        "pid": result["pid"],
        "remote_dir": remote_dir,
        "logs": {
            "stdout": str(PurePosixPath(remote_dir) / "stdout.log"),
            "stderr": str(PurePosixPath(remote_dir) / "stderr.log"),
            "status": str(PurePosixPath(remote_dir) / "status.json"),
            "meta": str(PurePosixPath(remote_dir) / "meta.json"),
            "local_record": str(_job_record_path(job_id)),
        },
    }


def _resolve_job_target(job_id: str, args: argparse.Namespace | None = None) -> tuple[RemoteTarget, dict[str, Any]]:
    job_id = require_safe_id(job_id, label="job id")
    record = _load_job_record(job_id)
    record_job_id = require_safe_id(str(record.get("job_id", "")), label="job id")
    if record_job_id != job_id:
        raise RemoteToolboxError(f"job record id mismatch: requested {job_id!r}, record has {record_job_id!r}")
    if args and (getattr(args, "machine", None) or getattr(args, "session_id", None) or getattr(args, "session_file", None)):
        target = resolve_remote_target(
            machine=getattr(args, "machine", None),
            session_id=getattr(args, "session_id", None),
            session_file=getattr(args, "session_file", None),
        )
    else:
        t = record.get("target", {})
        if t.get("session_id"):
            target = resolve_remote_target(session_id=t["session_id"], session_file=t.get("session_file"))
        else:
            target = resolve_remote_target(machine=t.get("alias") or t.get("target_id"))
    return target, record


def remote_job_status(target: RemoteTarget, record: dict[str, Any]) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    remote_dir = record["remote_dir"]
    script = f"""
set +e
status_path={shlex.quote(str(PurePosixPath(remote_dir) / "status.json"))}
pid_path={shlex.quote(str(PurePosixPath(remote_dir) / "pid"))}
if [ -f "$status_path" ]; then cat "$status_path"; else printf '{{"status":"unknown"}}\\n'; fi
if [ -f "$pid_path" ]; then pid=$(cat "$pid_path"); if kill -0 "$pid" 2>/dev/null; then echo '__PID_ALIVE__=1'; else echo '__PID_ALIVE__=0'; fi; fi
"""
    result = ssh_exec_raw(target.container_endpoint, script, timeout=20, check=False)
    lines = (result.stdout or "").splitlines()
    status_data: dict[str, Any] = {"status": "unknown"}
    pid_alive = None
    if lines:
        with contextlib.suppress(json.JSONDecodeError):
            status_data = json.loads(lines[0])
    for line in lines[1:]:
        if line.startswith("__PID_ALIVE__="):
            pid_alive = line.endswith("1")
    if status_data.get("status") == "running" and pid_alive is False:
        status_data["status"] = "failed"
        status_data["reason"] = "pid is no longer alive but job status was not finalized"
    top_status = status_data.get("status", "needs_repair")
    if top_status == "unknown":
        top_status = "needs_repair"
    return {
        "status": top_status,
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "error": status_data.get("reason") if top_status in {"failed", "timeout", "needs_repair", "cancelled"} else None,
        "job": {**record, "remote_status": status_data, "pid_alive": pid_alive},
        "logs": {
            "stdout": str(PurePosixPath(record["remote_dir"]) / "stdout.log"),
            "stderr": str(PurePosixPath(record["remote_dir"]) / "stderr.log"),
        },
    }


def remote_job_tail(target: RemoteTarget, record: dict[str, Any], *, lines: int = 80, stream: str = "both") -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    remote_dir = PurePosixPath(record["remote_dir"])
    commands: list[str] = []
    if stream in {"stdout", "both"}:
        commands.append(f"echo __STDOUT__; tail -n {int(lines)} {shlex.quote(str(remote_dir / 'stdout.log'))} 2>/dev/null || true")
    if stream in {"stderr", "both"}:
        commands.append(f"echo __STDERR__; tail -n {int(lines)} {shlex.quote(str(remote_dir / 'stderr.log'))} 2>/dev/null || true")
    result = ssh_exec_raw(target.container_endpoint, "\n".join(commands), timeout=20, check=False)
    return {
        "status": "ok" if result.returncode == 0 else "failed",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "job_id": record["job_id"],
        "tail": result.stdout,
        "stderr_tail": result.stderr,
        "logs": {
            "stdout": str(remote_dir / "stdout.log"),
            "stderr": str(remote_dir / "stderr.log"),
        },
    }


def remote_job_stop(target: RemoteTarget, record: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    remote_dir = PurePosixPath(record["remote_dir"])
    sig = "-9" if force else "-15"
    script = f"""
set +e
pid_path={shlex.quote(str(remote_dir / "pid"))}
if [ ! -f "$pid_path" ]; then printf '%s\\n' '{{"status":"failed","error":"pid file missing"}}'; exit 0; fi
pid=$(cat "$pid_path")
kill {sig} "$pid" 2>/dev/null || true
sleep 1
alive=0
kill -0 "$pid" 2>/dev/null && alive=1
if [ "$alive" -eq 0 ]; then
  cat > {shlex.quote(str(remote_dir / "status.json"))} <<EOF
{{"status":"cancelled","job_id":"{record["job_id"]}","exit_code":null,"finished_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}}
EOF
fi
printf '{{"alive":%s}}\\n' "$alive"
"""
    result = _remote_json(target.container_endpoint, script, timeout=20)
    return {
        "status": "cancelled" if result.get("alive") == 0 else "failed",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "job_id": record["job_id"],
        "result": result,
        "logs": {
            "stdout": str(remote_dir / "stdout.log"),
            "stderr": str(remote_dir / "stderr.log"),
        },
    }


def remote_manifest(target: RemoteTarget, remote_path: str, *, timeout: float | None = 120) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    remote_json_path = json.dumps(remote_path)
    script = f"""
python3 - <<'PY'
import hashlib
import json
import os
import pathlib
import stat
root = pathlib.Path({remote_json_path})
if not root.exists():
    print(json.dumps({{"status":"needs_input","error":"remote path does not exist","remote_path":str(root)}}))
    raise SystemExit(0)
files = []
def add_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    rel = "." if path == root else str(path.relative_to(root))
    files.append({{"relpath": rel, "path": str(path), "size": size, "sha256": h.hexdigest(), "mode": stat.S_IMODE(path.stat().st_mode)}})
if root.is_file():
    add_file(root)
else:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = pathlib.Path(dirpath) / name
            if path.is_file():
                add_file(path)
print(json.dumps({{"status":"ok","remote_path":str(root),"is_dir":root.is_dir(),"file_count":len(files),"files":files}}, sort_keys=True))
PY
"""
    result = _remote_json(target.container_endpoint, script, timeout=timeout)
    status = result.get("status", "failed")
    return {
        "status": status,
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "error": result.get("error"),
        "artifacts": {
            "manifest": result,
        },
        "logs": {},
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _remote_join(root: str, relpath: str) -> str:
    if relpath == ".":
        return root
    return str(PurePosixPath(root) / PurePosixPath(relpath))


def artifact_pull(
    target: RemoteTarget,
    *,
    remote_path: str,
    local_dir: Path,
    timeout: float | None = None,
) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    manifest_payload = remote_manifest(target, remote_path, timeout=timeout)
    manifest = manifest_payload.get("artifacts", {}).get("manifest", {})
    if manifest.get("status") != "ok":
        return {
            "status": manifest.get("status", "failed"),
            "target": target.to_dict(),
            "started_at": started_at,
            "duration_ms": duration_ms(start),
            "error": manifest.get("error"),
            "artifacts": {"manifest": manifest},
            "logs": {},
        }
    ensure_state_dir(local_dir)
    pulled: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for file_info in manifest.get("files", []):
        relpath = file_info["relpath"]
        local_path = local_dir / ("artifact" if relpath == "." else relpath)
        ensure_state_dir(local_path.parent)
        if local_path.exists() and _sha256_file(local_path) == file_info["sha256"]:
            skipped.append({"relpath": relpath, "local_path": str(local_path), "reason": "hash-match"})
            continue
        pending.append(file_info)
    if pending and manifest.get("is_dir") and _remote_tar_available(target, timeout=timeout):
        batch_result = _artifact_pull_tar_batch(
            target,
            remote_path=remote_path,
            local_dir=local_dir,
            files=pending,
            timeout=timeout,
        )
        if batch_result["status"] != "ok":
            return {
                **batch_result,
                "target": target.to_dict(),
                "started_at": started_at,
                "duration_ms": duration_ms(start),
            }
        pulled.extend(batch_result["artifacts"]["pulled"])
    else:
        for file_info in pending:
            single = _artifact_pull_single(target, remote_path=remote_path, local_dir=local_dir, file_info=file_info, timeout=timeout)
            if single["status"] != "ok":
                return {
                    **single,
                    "target": target.to_dict(),
                    "started_at": started_at,
                    "duration_ms": duration_ms(start),
                    "artifacts": {"manifest": manifest, "pulled": pulled, "skipped": skipped},
                    "logs": {},
                }
            pulled.append(single["artifact"])
    local_manifest = local_dir / "manifest.json"
    _atomic_write_json(local_manifest, manifest)
    return {
        "status": "ok",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "artifacts": {
            "remote_path": remote_path,
            "local_dir": str(local_dir),
            "local_manifest": str(local_manifest),
            "manifest": manifest,
            "pulled": pulled,
            "skipped": skipped,
        },
        "logs": {},
    }


def _remote_tar_available(target: RemoteTarget, *, timeout: float | None = None) -> bool:
    result = ssh_exec_raw(target.container_endpoint, "command -v tar >/dev/null 2>&1", timeout=timeout, check=False)
    return result.returncode == 0


def _artifact_pull_single(
    target: RemoteTarget,
    *,
    remote_path: str,
    local_dir: Path,
    file_info: dict[str, Any],
    timeout: float | None,
) -> dict[str, Any]:
    relpath = file_info["relpath"]
    local_path = local_dir / ("artifact" if relpath == "." else relpath)
    ensure_state_dir(local_path.parent)
    remote_file = _remote_join(remote_path, relpath)
    tmp = local_path.with_suffix(local_path.suffix + ".tmp")
    cmd = f"cat {shlex.quote(remote_file)}"
    result = ssh_exec_bytes(target.container_endpoint, cmd, timeout=timeout)
    if result.returncode != 0:
        return {
            "status": "failed",
            "error": f"failed to pull {remote_file}",
            "stderr_tail": tail_text(result.stderr.decode("utf-8", errors="replace")),
        }
    tmp.write_bytes(result.stdout)
    observed = _sha256_file(tmp)
    if observed != file_info["sha256"]:
        tmp.unlink(missing_ok=True)
        return {
            "status": "failed",
            "error": f"hash mismatch for {remote_file}",
            "expected_sha256": file_info["sha256"],
            "observed_sha256": observed,
        }
    os.replace(tmp, local_path)
    return {
        "status": "ok",
        "artifact": {
            "relpath": relpath,
            "local_path": str(local_path),
            "sha256": observed,
            "size": file_info["size"],
            "transport": "cat",
        },
    }


def _artifact_pull_tar_batch(
    target: RemoteTarget,
    *,
    remote_path: str,
    local_dir: Path,
    files: list[dict[str, Any]],
    timeout: float | None,
) -> dict[str, Any]:
    relpaths = [str(file_info["relpath"]) for file_info in files if file_info.get("relpath") != "."]
    if not relpaths:
        return {
            "status": "failed",
            "error": "tar batch pull requires directory-relative file paths",
            "artifacts": {"pulled": []},
            "logs": {},
        }
    arg_text = "\0".join(f"./{relpath}" for relpath in relpaths) + "\0"
    remote = (
        f"cd {shlex.quote(remote_path)} && "
        "python3 - <<'PY' | tar --null -T - -cf -\n"
        "import sys\n"
        f"sys.stdout.buffer.write({arg_text.encode('utf-8')!r})\n"
        "PY"
    )
    result = ssh_exec_bytes(target.container_endpoint, remote, timeout=timeout)
    if result.returncode != 0:
        return {
            "status": "failed",
            "error": "remote tar stream failed",
            "stderr_tail": tail_text(result.stderr.decode("utf-8", errors="replace")),
            "artifacts": {"pulled": []},
            "logs": {},
        }
    expected_by_rel = {str(item["relpath"]): item for item in files}
    pulled: list[dict[str, Any]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                relpath = member.name[2:] if member.name.startswith("./") else member.name
                if relpath not in expected_by_rel:
                    return {
                        "status": "failed",
                        "error": f"unexpected file in tar stream: {member.name}",
                        "artifacts": {"pulled": pulled},
                        "logs": {},
                    }
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                local_path = local_dir / relpath
                ensure_state_dir(local_path.parent)
                tmp = local_path.with_suffix(local_path.suffix + ".tmp")
                with tmp.open("wb") as fh:
                    shutil.copyfileobj(extracted, fh)
                observed = _sha256_file(tmp)
                expected = expected_by_rel[relpath]
                if observed != expected["sha256"]:
                    tmp.unlink(missing_ok=True)
                    return {
                        "status": "failed",
                        "error": f"hash mismatch for {relpath}",
                        "expected_sha256": expected["sha256"],
                        "observed_sha256": observed,
                        "artifacts": {"pulled": pulled},
                        "logs": {},
                    }
                os.replace(tmp, local_path)
                pulled.append({
                    "relpath": relpath,
                    "local_path": str(local_path),
                    "sha256": observed,
                    "size": expected["size"],
                    "transport": "tar",
                })
    except tarfile.TarError as exc:
        return {
            "status": "failed",
            "error": f"failed to read remote tar stream: {exc}",
            "artifacts": {"pulled": pulled},
            "logs": {},
        }
    missing = sorted(set(expected_by_rel) - {item["relpath"] for item in pulled})
    if missing:
        return {
            "status": "failed",
            "error": "remote tar stream missed expected files",
            "missing": missing,
            "artifacts": {"pulled": pulled},
            "logs": {},
        }
    return {"status": "ok", "artifacts": {"pulled": pulled}, "logs": {}}


def _local_manifest(local_path: Path) -> dict[str, Any]:
    raw_path = local_path.expanduser()
    if raw_path.is_symlink():
        raise RemoteToolboxError("symlinks are not allowed by artifact_push")
    local_path = raw_path.resolve()
    if not local_path.exists():
        raise RemoteToolboxError(f"local path does not exist: {local_path}")
    files: list[dict[str, Any]] = []
    roots = [local_path] if local_path.is_file() else sorted(p for p in local_path.rglob("*") if p.is_file())
    for path in roots:
        if path.is_symlink():
            raise RemoteToolboxError(f"symlinks are not allowed by artifact_push: {path}")
        relpath = "." if path == local_path else str(path.relative_to(local_path))
        files.append({
            "relpath": relpath,
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        })
    return {"status": "ok", "local_path": str(local_path), "is_dir": local_path.is_dir(), "file_count": len(files), "files": files}


def artifact_push(
    target: RemoteTarget,
    *,
    local_path: Path,
    remote_path: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    started_at = now_iso()
    start = time.monotonic()
    manifest = _local_manifest(local_path)
    pushed: list[dict[str, Any]] = []
    for file_info in manifest["files"]:
        relpath = file_info["relpath"]
        src = Path(file_info["path"])
        remote_file = _remote_join(remote_path, relpath)
        remote_tmp = f"{remote_file}.tmp-{uuid.uuid4().hex[:8]}"
        mkdir_script = f"mkdir -p {shlex.quote(str(PurePosixPath(remote_file).parent))}"
        mkdir_result = ssh_exec_raw(target.container_endpoint, mkdir_script, timeout=timeout, check=False)
        if mkdir_result.returncode != 0:
            return {
                "status": "failed",
                "target": target.to_dict(),
                "started_at": started_at,
                "duration_ms": duration_ms(start),
                "error": f"failed to create remote dir for {remote_file}",
                "stderr_tail": mkdir_result.stderr,
                "artifacts": {"manifest": manifest, "pushed": pushed},
                "logs": {},
            }
        with src.open("rb") as fh:
            upload = ssh_exec_bytes(
                target.container_endpoint,
                f"cat > {shlex.quote(remote_tmp)} && sha256sum {shlex.quote(remote_tmp)} | awk '{{print $1}}'",
                stdin=fh,
                timeout=timeout,
            )
        observed = upload.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [""]
        if upload.returncode != 0 or observed[0] != file_info["sha256"]:
            ssh_exec_raw(target.container_endpoint, f"rm -f {shlex.quote(remote_tmp)}", check=False)
            return {
                "status": "failed",
                "target": target.to_dict(),
                "started_at": started_at,
                "duration_ms": duration_ms(start),
                "error": f"failed to push or verify {src}",
                "expected_sha256": file_info["sha256"],
                "observed_sha256": observed[0],
                "stderr_tail": upload.stderr.decode("utf-8", errors="replace")[-2000:],
                "artifacts": {"manifest": manifest, "pushed": pushed},
                "logs": {},
            }
        mv = ssh_exec_raw(target.container_endpoint, f"mv -f {shlex.quote(remote_tmp)} {shlex.quote(remote_file)}", timeout=timeout, check=False)
        if mv.returncode != 0:
            return {
                "status": "failed",
                "target": target.to_dict(),
                "started_at": started_at,
                "duration_ms": duration_ms(start),
                "error": f"failed to finalize {remote_file}",
                "stderr_tail": mv.stderr,
                "artifacts": {"manifest": manifest, "pushed": pushed},
                "logs": {},
            }
        pushed.append({"relpath": relpath, "remote_path": remote_file, "sha256": file_info["sha256"], "size": file_info["size"]})
    return {
        "status": "ok",
        "target": target.to_dict(),
        "started_at": started_at,
        "duration_ms": duration_ms(start),
        "artifacts": {"remote_path": remote_path, "manifest": manifest, "pushed": pushed},
        "logs": {},
    }


def _run_json_command(cmd: list[str], *, cwd: Path = ROOT, relay_stderr: bool = True) -> tuple[int, dict[str, Any], str, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stderr is not None
    stderr_parts: list[str] = []

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)
            if relay_stderr:
                sys.stderr.write(line)
                sys.stderr.flush()

    thread = threading.Thread(target=read_stderr, daemon=True)
    thread.start()
    assert proc.stdout is not None
    stdout = proc.stdout.read()
    rc = proc.wait()
    thread.join(timeout=1)
    stderr = "".join(stderr_parts)
    try:
        payload = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"status": "failed", "error": "subcommand returned non-JSON stdout", "stdout_tail": tail_text(stdout)}
    return rc, payload, stdout, stderr


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
    rc, payload, stdout, stderr = _run_json_command(cmd, relay_stderr=True)
    if rc != 0:
        raise RemoteToolboxError(f"failed to derive parity args: stdout={tail_text(stdout)} stderr={tail_text(stderr)}")
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
    rc, payload, stdout, stderr = _run_json_command(cmd, relay_stderr=True)
    if rc != 0:
        raise RemoteToolboxError(f"failed to build parity plan: stdout={tail_text(stdout)} stderr={tail_text(stderr)}")
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
    will_install = mode == "install" and (
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


def sync_apply(target: RemoteTarget, *, mode: str, force_reinstall: bool = False, dry_run: bool = False) -> dict[str, Any]:
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
    rc, payload, stdout, stderr = _run_json_command(cmd, relay_stderr=True)
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
        raise RemoteToolboxError(f"unsupported service action: {action}")
    cmd = [sys.executable, str(script_map[action])]
    if target.session_file:
        cmd.extend(["--session-file", str(target.session_file)])
    elif target.session_id:
        cmd.extend(["--session-id", target.session_id])
    else:
        cmd.extend(["--machine", target.alias])
    cmd.extend(extra_args)
    rc, payload, stdout, stderr = _run_json_command(cmd, relay_stderr=True)
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
    result = ssh_exec_raw(target.container_endpoint, "\n".join(chunks), timeout=30, check=False)
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
                    str(PurePosixPath(target.remote_toolbox_root()) / "jobs" / require_remote_leaf(job_id, label="job id"))
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
            result = ssh_exec_raw(target.container_endpoint, script, timeout=60, check=False)
            actions.append({"action": "remote-rm", "paths": remote_paths, "returncode": result.returncode, "stderr_tail": tail_text(result.stderr)})
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
            rc, payload, stdout, stderr = _run_json_command(cmd, relay_stderr=True)
            actions.append({"action": "session-remove", "returncode": rc, "result": payload, "stdout_tail": tail_text(stdout), "stderr_tail": tail_text(stderr)})
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
            release_all_session_leases(repo_root=target.state_repo_root, session_id=target.session_id)
            actions.append({"action": "release-leases", "session_id": target.session_id, "released": True})
    if known_hosts:
        for endpoint in (target.host_endpoint, target.container_endpoint):
            key = endpoint.known_hosts_key()
            if dry_run:
                actions.append({"action": "known-hosts-remove", "key": key, "dry_run": True})
            else:
                result = subprocess.run(["ssh-keygen", "-R", key], capture_output=True, text=True, check=False)
                actions.append({"action": "known-hosts-remove", "key": key, "returncode": result.returncode, "stderr_tail": tail_text(result.stderr)})
    proof = {
        "target_after_cleanup": target.to_dict(),
        "known_hosts": {
            "host": known_hosts_status(target.host_endpoint),
            "container": known_hosts_status(target.container_endpoint),
        },
    }
    if target.session_id:
        with contextlib.suppress(Exception):
            proof["leases"] = load_leases(target.state_repo_root)
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


def _cli_error(exc: BaseException, *, started_at: str, start: float) -> int:
    status = "failed"
    if isinstance(exc, (RemoteToolboxError, WorkspaceStateError, ValidationError, FileNotFoundError)):
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


def cli_target_resolve(argv: Sequence[str] | None = None) -> int:
    started_at = now_iso()
    start = time.monotonic()
    parser = argparse.ArgumentParser(description="Resolve a VAWS remote target.", allow_abbrev=False)
    add_target_args(parser)
    args = parser.parse_args(argv)
    try:
        target = target_from_args(args)
        print_json({"status": "ok", "target": target.to_dict(), "started_at": started_at, "duration_ms": duration_ms(start), "logs": {}})
        return 0
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_probe(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe a VAWS remote target.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        print_json(probe_remote(target_from_args(args), timeout=args.timeout))
        return 0
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_exec(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute a shell command on a VAWS remote target.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--cwd")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--timeout", type=float)
    parser.add_argument(
        "--no-runtime-env",
        action="store_true",
        help="do not source /etc/profile.d/vaws-ascend-env.sh before running the command",
    )
    parser.add_argument("--command", help="shell command to execute; alternatively pass after --")
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        command = args.command
        if not command and args.command_args:
            command_args = list(args.command_args)
            if command_args and command_args[0] == "--":
                command_args = command_args[1:]
            command = " ".join(command_args)
        if not command:
            raise RemoteToolboxError("--command or command after -- is required")
        env = _parse_env_items(args.env)
        payload = remote_exec(
            target_from_args(args),
            command=command,
            cwd=args.cwd,
            env=env,
            timeout=args.timeout,
            runtime_env=not args.no_runtime_env,
        )
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_job_start(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start a long-running remote job.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--cwd")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--kind", default="command")
    parser.add_argument("--job-id")
    parser.add_argument("--timeout", type=int)
    parser.add_argument(
        "--no-runtime-env",
        action="store_true",
        help="do not source /etc/profile.d/vaws-ascend-env.sh before running the job",
    )
    parser.add_argument("--command", required=True)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        env = _parse_env_items(args.env)
        job_id = require_safe_id(args.job_id, label="job id") if args.job_id else None
        print_json(start_remote_job(
            target_from_args(args),
            command=args.command,
            cwd=args.cwd,
            env=env,
            kind=args.kind,
            timeout_seconds=args.timeout,
            job_id=job_id,
            runtime_env=not args.no_runtime_env,
        ))
        return 0
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_job_status(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect a remote job.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        target, record = _resolve_job_target(args.job_id, args)
        payload = remote_job_status(target, record)
        print_json(payload)
        return 0 if payload["status"] in {"running", "succeeded", "ok"} else 1
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_job_tail(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tail a remote job log.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--stream", choices=("stdout", "stderr", "both"), default="both")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        target, record = _resolve_job_target(args.job_id, args)
        print_json(remote_job_tail(target, record, lines=args.lines, stream=args.stream))
        return 0
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_job_stop(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop a remote job.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        target, record = _resolve_job_target(args.job_id, args)
        payload = remote_job_stop(target, record, force=args.force)
        print_json(payload)
        return 0 if payload["status"] == "cancelled" else 1
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_job_collect(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect a remote job directory.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--local-dir", type=Path)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        target, record = _resolve_job_target(args.job_id, args)
        local_dir = args.local_dir or (ARTIFACT_STATE_DIR / "jobs" / args.job_id)
        print_json(artifact_pull(target, remote_path=record["remote_dir"], local_dir=local_dir))
        return 0
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_sync_plan(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan remote code sync without mutating runtime.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--mode", choices=("source-only", "materialize", "install"), required=True)
    parser.add_argument("--force-reinstall", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        print_json(sync_plan(target_from_args(args), mode=args.mode, force_reinstall=args.force_reinstall))
        return 0
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_sync_apply(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply remote code sync in a selected mode.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--mode", choices=("source-only", "materialize", "install"), required=True)
    parser.add_argument("--force-reinstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = sync_apply(target_from_args(args), mode=args.mode, force_reinstall=args.force_reinstall, dry_run=args.dry_run)
        print_json(payload)
        return 0 if payload["status"] in {"ready", "source-only", "materialized", "dry-run", "ok", "skipped"} else 1
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_service_start(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start vLLM service through the remote toolbox.",
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
        return _cli_error(exc, started_at=started_at, start=start)


def cli_service_status(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check vLLM service status through the remote toolbox.", allow_abbrev=False)
    add_target_args(parser)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = call_service("status", target_from_args(args), [])
        print_json(payload)
        return 0 if payload["status"] in {"ready", "alive", "alive_healthy", "stopped", "not_found"} else 1
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_service_logs(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tail vLLM service logs through the remote toolbox.", allow_abbrev=False)
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
        return _cli_error(exc, started_at=started_at, start=start)


def cli_service_stop(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop vLLM service through the remote toolbox.", allow_abbrev=False)
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
        return _cli_error(exc, started_at=started_at, start=start)


def cli_artifact_manifest(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a remote artifact manifest.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--remote-path", required=True)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = remote_manifest(target_from_args(args), args.remote_path)
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_artifact_pull(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull remote artifacts through SSH streaming.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--remote-path", required=True)
    parser.add_argument("--local-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = artifact_pull(target_from_args(args), remote_path=args.remote_path, local_dir=args.local_dir)
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_artifact_push(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push local artifacts through SSH streaming.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--local-path", type=Path, required=True)
    parser.add_argument("--remote-path", required=True)
    args = parser.parse_args(argv)
    started_at = now_iso()
    start = time.monotonic()
    try:
        payload = artifact_push(target_from_args(args), local_path=args.local_path, remote_path=args.remote_path)
        print_json(payload)
        return 0 if payload["status"] == "ok" else 1
    except Exception as exc:  # noqa: BLE001
        return _cli_error(exc, started_at=started_at, start=start)


def cli_cleanup(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean remote toolbox/session state.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jobs", action="store_true")
    parser.add_argument("--job-id", action="append", default=[], help="cleanup only this remote-toolbox job id (repeatable)")
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
        return _cli_error(exc, started_at=started_at, start=start)
