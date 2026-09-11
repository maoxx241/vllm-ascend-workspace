#!/usr/bin/env python3
"""Shared utilities for ascend-profiling-collection scripts.

This module owns helpers that exist *because of profiling*: the SSH +
ascend-env preamble, the local SSH tunnel for sending workload requests, the
profile-control client, and progress / state-dir conventions.

Inventory resolution and SSH primitives are reused from the serving skill's
``_common`` so we do not maintain a second copy.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
SERVING_SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_result_envelope import progress as envelope_progress  # noqa: E402

COLLECTION_STATE_DIR = ROOT / ".vaws-local" / "ascend-profiling-collection" / "runs"


# ---------------------------------------------------------------------------
# Lazy import of serving _common (single source of truth for SSH + inventory)
# ---------------------------------------------------------------------------

def _load_serving_common():
    """Load the serving skill's _common.py without polluting sys.path.

    We import it as ``vaws_profcoll_serving_common`` so it does not collide
    with this skill's own ``_common`` module name.
    """
    module_name = "vaws_profcoll_serving_common"
    if module_name in sys.modules:
        return sys.modules[module_name]
    src = SERVING_SCRIPTS / "_serving_common.py"
    spec = importlib.util.spec_from_file_location(module_name, src)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load serving common helpers from {src}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


SERVING = _load_serving_common()
# Loading serving common put LIB_DIR on sys.path.
from vaws_local_state import allocate_run_dir, safe_run_token  # noqa: E402
from vaws_remote_dev import open_local_forward, require_transport  # noqa: E402
from vaws_remote_target import ascend_env_preamble  # noqa: E402

SshEndpoint = SERVING.SshEndpoint
ssh_exec = SERVING.ssh_exec
endpoint_from_reply = SERVING.endpoint_from_reply
service_port_of = SERVING.service_port_of


@dataclass
class ExecutionTarget:
    mode: str
    alias: str
    endpoint: Any
    execution_id: str | None = None
    task_id: str | None = None
    python: str | None = None
    cwd: str | None = None
    session_id: str | None = None
    session_file: str | None = None


def resolve_execution_target(*, context_file=None, execution_id=None, host=None, port=None, user="root", service="vllm"):
    from vaws_remote_target import SshEndpoint as Endpoint
    from vaws_task_target import executions_for_service, execution_target, task_client, task_id_of

    if host:
        ep = Endpoint(host=host, port=int(port or 22), user=user)
        return ExecutionTarget(mode="endpoint", alias=host, endpoint=ep)
    client = task_client(context_file)
    if not execution_id:
        rows = executions_for_service(client, service)
        if not rows:
            raise RuntimeError("pass --execution-id or --host")
        execution_id = str(rows[-1].get("id") or rows[-1].get("execution_id"))
    target = execution_target(client, str(execution_id))
    endpoint = endpoint_from_reply({"target": target})
    cwd = (target.get("endpoint") or {}).get("cwd") or (target.get("endpoint") or {}).get("root")
    return ExecutionTarget(
        mode="execution",
        alias=str(target.get("container_name") or endpoint.host),
        endpoint=endpoint,
        execution_id=str(execution_id),
        task_id=task_id_of(client),
        python=target.get("python"),
        cwd=cwd,
        session_id=task_id_of(client),
    )


# ---------------------------------------------------------------------------
# Progress / output
# ---------------------------------------------------------------------------

def emit_progress(phase: str, message: str, **extra: Any) -> None:
    envelope_progress(phase, message, **extra)


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def unique_collection_run_dir(
    *,
    tag: str,
    session_id: str | None = None,
    machine: str | None = None,
) -> Path:
    """Allocate ``<COLLECTION_STATE_DIR>/<utc-ts>_<tag>_<target>`` (collision-safe).

    ``safe_run_token`` / ``allocate_run_dir`` come from ``.agents/lib``; this
    wrapper only adds the collection-specific tag+target naming.
    """
    target_token = safe_run_token(session_id or machine or "target", fallback="target")
    tag_token = safe_run_token(tag, fallback="profile")
    return allocate_run_dir(COLLECTION_STATE_DIR, f"{tag_token}_{target_token}")


# ---------------------------------------------------------------------------
# Ascend env preamble (canonical form lives in vaws_remote_target)
# ---------------------------------------------------------------------------

ASCEND_ENV_PREAMBLE = ascend_env_preamble()


# ---------------------------------------------------------------------------
# Local SSH tunnel for sending workload requests from the local machine
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def open_local_tunnel(ep, remote_port: int):
    """Open an ephemeral ``ssh -L`` tunnel to ``127.0.0.1:<remote_port>``.

    Yields a dict with ``local_port`` and ``base_url``. Used by the workload
    sender so multimodal payloads (image data URLs) can be assembled locally
    and POSTed without round-tripping through SSH heredocs. Transport lives
    in ``vaws-remote-dev`` ``open_local_forward``.
    """
    api = require_transport()
    try:
        with open_local_forward(ep, remote_port) as fwd:
            yield {
                "local_port": int(fwd.local_port),
                "base_url": f"http://{fwd.local_host}:{fwd.local_port}",
            }
    except api["RemoteExecutionError"] as exc:
        raise RuntimeError(str(exc)) from exc


# ---------------------------------------------------------------------------
# JSON-emitting subprocess wrapper for sibling scripts
# ---------------------------------------------------------------------------

def call_json_command(cmd: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
    """Run ``cmd`` and parse its stdout as JSON.

    Stderr is always relayed so the agent sees progress markers from the
    underlying serving / parity scripts. Raises RuntimeError on non-zero exit
    or non-JSON output, with both streams attached.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd or ROOT),
    )
    stderr_lines: list[str] = []

    def relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()

    thread = threading.Thread(target=relay_stderr, daemon=True)
    thread.start()
    assert proc.stdout is not None
    stdout = proc.stdout.read()
    returncode = proc.wait()
    thread.join(timeout=1)
    stderr = "".join(stderr_lines)
    if returncode != 0:
        raise RuntimeError(
            f"command failed (rc={returncode}): {' '.join(cmd)}\n"
            f"stdout={stdout[:2000]}\nstderr={stderr[:2000]}"
        )
    if not stdout.strip():
        raise RuntimeError(
            f"command produced no output: {' '.join(cmd)}\n"
            f"stderr={stderr[:2000]}"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"command returned non-JSON output: {' '.join(cmd)}\n"
            f"stdout={stdout[:2000]}"
        ) from exc


# ---------------------------------------------------------------------------
# Convenience wrappers around the serving skill's CLI scripts
# ---------------------------------------------------------------------------

def call_serve_start(extra_args: list[str]) -> dict[str, Any]:
    cmd = [sys.executable, str(SERVING_SCRIPTS / "serve_start.py"), *extra_args]
    return call_json_command(cmd)


def call_serve_stop(
    *,
    context_file: str | None = None,
    execution_id: str | None = None,
    service: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    cmd = [sys.executable, str(SERVING_SCRIPTS / "serve_stop.py")]
    if context_file:
        cmd.extend(["--context-file", context_file])
    if execution_id:
        cmd.extend(["--execution-id", execution_id])
    if service:
        cmd.extend(["--service", service])
    if force:
        cmd.append("--force")
    return call_json_command(cmd)
