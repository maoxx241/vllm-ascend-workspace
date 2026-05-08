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
import socket
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
MM_SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"
SERVING_SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"

PROGRESS_SENTINEL = "__VAWS_PROFILING_COLLECTION_PROGRESS__="
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
    src = SERVING_SCRIPTS / "_common.py"
    spec = importlib.util.spec_from_file_location(module_name, src)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load serving common helpers from {src}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


SERVING = _load_serving_common()
SshEndpoint = SERVING.SshEndpoint
ssh_exec = SERVING.ssh_exec
resolve_machine = SERVING.resolve_machine
container_endpoint = SERVING.container_endpoint
host_endpoint = SERVING.host_endpoint
load_serving_state = SERVING.load_serving_state


# ---------------------------------------------------------------------------
# Progress / output
# ---------------------------------------------------------------------------

def emit_progress(phase: str, message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"phase": phase, "message": message}
    payload.update({k: v for k, v in extra.items() if v is not None})
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


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


# ---------------------------------------------------------------------------
# Ascend env preamble (mirrors the one in benchmark/_common.py)
# ---------------------------------------------------------------------------

ASCEND_ENV_PREAMBLE = textwrap.dedent(
    """\
    set -e
    if [ -f /etc/profile.d/vaws-ascend-env.sh ]; then
      set +u
      source /etc/profile.d/vaws-ascend-env.sh
      set -u
    fi
    """
).rstrip("\n")


# ---------------------------------------------------------------------------
# Local SSH tunnel for sending workload requests from the local machine
# ---------------------------------------------------------------------------

def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def open_local_tunnel(ep, remote_port: int):
    """Open an ephemeral ``ssh -L`` tunnel to ``127.0.0.1:<remote_port>``.

    Yields a dict with ``local_port`` and ``base_url``. Used by the workload
    sender so multimodal payloads (image data URLs) can be assembled locally
    and POSTed without round-tripping through SSH heredocs.
    """
    local_port = _find_free_local_port()
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "LogLevel=ERROR",
        "-N",
        "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        "-p", str(ep.port),
        ep.destination(),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 15
        last_error = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                raise RuntimeError(
                    f"ssh tunnel exited early (rc={proc.returncode}): {stderr[:2000]}"
                )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                try:
                    sock.connect(("127.0.0.1", local_port))
                    yield {
                        "local_port": local_port,
                        "base_url": f"http://127.0.0.1:{local_port}",
                    }
                    return
                except OSError as exc:
                    last_error = str(exc)
                    time.sleep(0.2)
        raise RuntimeError(
            f"timed out waiting for ssh tunnel to localhost:{local_port} ({last_error})"
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# JSON-emitting subprocess wrapper for sibling scripts
# ---------------------------------------------------------------------------

def call_json_command(cmd: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
    """Run ``cmd`` and parse its stdout as JSON.

    Stderr is always relayed so the agent sees progress markers from the
    underlying serving / parity scripts. Raises RuntimeError on non-zero exit
    or non-JSON output, with both streams attached.
    """
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd or ROOT),
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
        sys.stderr.flush()
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed (rc={result.returncode}): {' '.join(cmd)}\n"
            f"stdout={result.stdout[:2000]}\nstderr={result.stderr[:2000]}"
        )
    if not result.stdout.strip():
        raise RuntimeError(
            f"command produced no output: {' '.join(cmd)}\n"
            f"stderr={result.stderr[:2000]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"command returned non-JSON output: {' '.join(cmd)}\n"
            f"stdout={result.stdout[:2000]}"
        ) from exc


# ---------------------------------------------------------------------------
# Convenience wrappers around the serving skill's CLI scripts
# ---------------------------------------------------------------------------

def call_serve_start(extra_args: list[str]) -> dict[str, Any]:
    cmd = [sys.executable, str(SERVING_SCRIPTS / "serve_start.py"), *extra_args]
    return call_json_command(cmd)


def call_serve_stop(machine: str, *, force: bool = False) -> dict[str, Any]:
    cmd = [sys.executable, str(SERVING_SCRIPTS / "serve_stop.py"), "--machine", machine]
    if force:
        cmd.append("--force")
    return call_json_command(cmd)
