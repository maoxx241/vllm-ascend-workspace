#!/usr/bin/env python3
"""Start a vllm-ascend online service on a session-managed remote container.

Serving is session-only. Inside a session worktree the session is
auto-resolved, so no target flag is needed.

Usage examples:

    # From inside a session worktree (auto-resolved session)
    python3 serve_start.py --model /data/models/Qwen3-32B --tp 4

    # Explicit session target
    python3 serve_start.py --session-id pr-123 --model /data/models/Qwen3-32B \\
        --tp 4 --devices 0,1,2,3 -- --max-model-len 4096

    # Relaunch with same config
    python3 serve_start.py --session-id pr-123 --relaunch

    # Relaunch with a new env variable
    python3 serve_start.py --session-id pr-123 --relaunch --extra-env VLLM_USE_V1=1

Progress on stderr as __VAWS_SERVING_PROGRESS__=<json>.
Final result on stdout as a single JSON object.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _common import (
    ROOT,
    SshEndpoint,
    emit_progress,
    load_preset,
    load_serving_state,
    now_utc,
    print_json,
    probe_npus,
    resolve_execution_target,
    save_serving_state,
    select_devices,
    ssh_exec,
)
from vaws_session_state import allocate_service_port, file_lock, release_service_port, session_lock_dir, require_session_npu_lease, SessionStateError
from vaws_local_state import effective_workspace_alias, load_workspace_identity
from vaws_validate import parse_device_csv, require_env_name

RUNTIME_DIR_BASE = ".vaws-runtime/serving"
DEFAULT_HEALTH_TIMEOUT = 300
HEALTH_POLL_INTERVAL = 5
PORT_TAIL_RE = re.compile(r"[:.]([0-9]+)$")
# Backstop for the parity subprocess; parity itself bounds git transport at
# 900s, so an hour only fires on a genuinely stuck child.
PARITY_TIMEOUT_SECONDS = 3600


def service_runtime_dir(runtime_base: str, instance_ts: str, alias: str | None) -> str:
    runtime_namespace = f"/{alias}" if alias else ""
    return f"{runtime_base}/{RUNTIME_DIR_BASE}{runtime_namespace}/{instance_ts}"


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------

def run_parity(session_id: str, session_file: Path | None = None) -> dict[str, Any]:
    parity_script = ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts" / "parity_sync.py"
    if session_file is not None:
        cmd = [sys.executable, str(parity_script), "--session-file", str(session_file)]
    else:
        cmd = [sys.executable, str(parity_script), "--session-id", session_id]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if line.startswith("__VAWS_PARITY_PROGRESS__="):
                sys.stderr.write(line)
                sys.stderr.flush()

    def collect_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_lines.append(line)

    thread = threading.Thread(target=relay_stderr, daemon=True)
    thread.start()
    stdout_thread = threading.Thread(target=collect_stdout, daemon=True)
    stdout_thread.start()
    try:
        returncode = proc.wait(timeout=PARITY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        thread.join(timeout=1)
        stdout_thread.join(timeout=1)
        return {
            "status": "failed",
            "error": f"parity sync timed out after {PARITY_TIMEOUT_SECONDS}s",
            "stderr_tail": "".join(stderr_lines)[-1000:],
        }
    thread.join(timeout=1)
    stdout_thread.join(timeout=1)
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    if returncode != 0:
        return {
            "status": "failed",
            "error": f"parity sync failed (rc={returncode})",
            "stderr_tail": stderr[-1000:],
        }
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "status": "failed",
            "error": "parity sync returned non-JSON output",
            "stdout_tail": (stdout or "")[-500:],
        }


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

def remote_port_available(ep: SshEndpoint, port: int) -> bool:
    script = (
        "python3 -c "
        + shlex.quote(
            "import socket,sys\n"
            f"port={int(port)}\n"
            "s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "try:\n"
            "    s.bind(('0.0.0.0', port))\n"
            "except OSError:\n"
            "    sys.exit(1)\n"
            "finally:\n"
            "    s.close()\n"
        )
    )
    return ssh_exec(ep, script, check=False).returncode == 0


def _parse_listening_ports(stdout: str) -> set[int]:
    ports: set[int] = set()
    for line in stdout.splitlines():
        match = PORT_TAIL_RE.search(line.strip())
        if match:
            ports.add(int(match.group(1)))
    return ports


def remote_listening_ports(ep: SshEndpoint) -> set[int] | None:
    script = """
if command -v ss >/dev/null 2>&1; then
  ss -ltnH 2>/dev/null | awk '{print $4}'
elif command -v netstat >/dev/null 2>&1; then
  netstat -ltn 2>/dev/null | awk 'NR > 2 {print $4}'
else
  exit 42
fi
"""
    result = ssh_exec(ep, script, check=False)
    if result.returncode != 0:
        return None
    return _parse_listening_ports(result.stdout)


def remote_port_availability(ep: SshEndpoint):
    busy_ports = remote_listening_ports(ep)
    if busy_ports is None:
        return lambda candidate: remote_port_available(ep, candidate)
    return lambda candidate: candidate not in busy_ports


def _parse_devices_csv(value: str) -> set[int]:
    if not value or not value.strip():
        return set()
    return set(parse_device_csv(value) or [])


# ---------------------------------------------------------------------------
# Launch script builder (the core escaping-safe layer)
# ---------------------------------------------------------------------------

def _require_heredoc_safe(value: str, label: str) -> str:
    """Reject values that could break out of the launch script heredoc.

    The vllm command is written into ``_serve.sh`` through a quoted heredoc.
    ``shlex.quote`` does not help there: the heredoc body is literal text, so
    a newline inside a token would split the ``exec`` line — and a line
    matching the ``VAWS_SERVE_EOF`` delimiter would end the heredoc early and
    execute the remainder as shell. Fail fast instead.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must not contain newline characters")
    return value


def build_launch_script(
    *,
    runtime_dir: str,
    model: str,
    served_model_name: str,
    port: int,
    tp: int | None,
    dp: int | None,
    devices: str | None,
    extra_env: dict[str, str],
    extra_args: list[str],
    wrap_script: str = "",
) -> str:
    lines: list[str] = ["set -e"]

    lines.append(f"mkdir -p {shlex.quote(runtime_dir)}")

    # Ascend environment — source the managed profile that sets PATH,
    # LD_LIBRARY_PATH, CANN, ATB, and the correct Python.
    lines.append(
        "if [ -f /etc/profile.d/vaws-ascend-env.sh ]; then"
        "  set +u; source /etc/profile.d/vaws-ascend-env.sh; set -u;"
        " fi"
    )
    lines.append(
        'export LD_LIBRARY_PATH='
        '"/usr/local/Ascend/driver/lib64/driver'
        ':/usr/local/Ascend/driver/lib64'
        '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'
    )

    # vllm-ascend custom CANN operators (aclnnAddRmsNormBias etc.)
    # Locate set_env.bash dynamically — vendor name may change across versions.
    lines.append(
        '_CUST_BASE=$(python3 -c '
        '"import vllm_ascend,os;print(os.path.join(os.path.dirname(vllm_ascend.__file__),'
        '\'_cann_ops_custom\'))" 2>/dev/null || true)'
    )
    lines.append(
        'if [ -n "$_CUST_BASE" ] && [ -d "$_CUST_BASE" ]; then'
        '  _CUST_ENV=$(find "$_CUST_BASE" -name set_env.bash -path "*/bin/set_env.bash" 2>/dev/null | head -1);'
        '  if [ -n "$_CUST_ENV" ]; then set +u; source "$_CUST_ENV"; set -u; fi;'
        " fi"
    )

    if devices:
        lines.append(f"export ASCEND_RT_VISIBLE_DEVICES={shlex.quote(devices)}")

    for key, value in extra_env.items():
        name = require_env_name(key)
        lines.append(f"export {name}={shlex.quote(value)}")

    # Launch from the runtime dir — NOT from /vllm-workspace, which would
    # shadow the installed vllm package with the source tree.
    lines.append(f"cd {shlex.quote(runtime_dir)}")

    # Build argv — every token individually quoted for bash safety.
    # Tokens land inside a quoted heredoc, where shlex.quote cannot contain
    # newlines, so heredoc-bound values are validated first.
    argv_tokens = ["vllm", "serve", shlex.quote(_require_heredoc_safe(model, "--model"))]
    argv_tokens.extend(["--host", "0.0.0.0"])
    argv_tokens.extend(["--port", str(port)])
    if served_model_name:
        argv_tokens.extend([
            "--served-model-name",
            shlex.quote(_require_heredoc_safe(served_model_name, "--served-model-name")),
        ])
    if tp is not None:
        argv_tokens.extend(["--tensor-parallel-size", str(tp)])
    if dp is not None:
        argv_tokens.extend(["--data-parallel-size", str(dp)])
    for arg in extra_args:
        argv_tokens.append(shlex.quote(_require_heredoc_safe(arg, "extra vllm arg")))

    cmd_str = " ".join(argv_tokens)
    stdout_log = f"{runtime_dir}/stdout.log"
    stderr_log = f"{runtime_dir}/stderr.log"
    pid_file = f"{runtime_dir}/pid"

    # Always write the vLLM command as a standalone script for clean quoting
    serve_script = f"{runtime_dir}/_serve.sh"
    lines.append(f"cat > {shlex.quote(serve_script)} << 'VAWS_SERVE_EOF'")
    lines.append("#!/bin/bash")
    lines.append(f"exec {cmd_str}")
    lines.append("VAWS_SERVE_EOF")
    lines.append(f"chmod +x {shlex.quote(serve_script)}")

    if wrap_script:
        # External wrapper: receives serve script path and runtime dir as args.
        # The wrapper decides how to launch (e.g. msprof wrapping, strace, etc.)
        lines.append(
            f"nohup bash {shlex.quote(wrap_script)}"
            f" {shlex.quote(serve_script)} {shlex.quote(runtime_dir)}"
            f" > {shlex.quote(stdout_log)}"
            f" 2> {shlex.quote(stderr_log)}"
            f" </dev/null &"
        )
    else:
        lines.append(
            f"nohup bash {shlex.quote(serve_script)}"
            f" > {shlex.quote(stdout_log)}"
            f" 2> {shlex.quote(stderr_log)}"
            f" </dev/null &"
        )

    lines.append("_PID=$!")
    lines.append("disown $_PID")
    lines.append(f"echo $_PID > {shlex.quote(pid_file)}")
    lines.append("echo $_PID")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def check_alive(ep: SshEndpoint, pid: int) -> bool:
    r = ssh_exec(ep, f"kill -0 {pid} 2>/dev/null && echo alive || echo dead", check=False)
    if r.returncode != 0 or r.stdout.strip() not in ("alive", "dead"):
        raise RuntimeError("service process state is unknown; SSH failure is not proof of exit")
    return r.stdout.strip() == "alive"


def wait_for_devices_free(host_ep: SshEndpoint, devices: set[int], *, timeout: int = 45) -> bool:
    if not devices:
        return True
    deadline = time.time() + timeout
    while True:
        try:
            npu_info = probe_npus(host_ep)
            busy = {int(dev) for dev in npu_info.get("busy", {}) if str(dev).isdigit()}
            if not devices.issubset({int(dev) for dev in npu_info.get("devices", [])}):
                return False
            if not (devices & busy):
                return True
        except Exception as exc:
            # A failed probe means we cannot confirm the devices are free.
            # Fail closed (report "not free") rather than masking a possibly
            # still-busy device as available.
            sys.stderr.write(f"[serve_start] device-free probe failed: {exc}\n")
            return False
        if time.time() >= deadline:
            return False
        time.sleep(3)


def read_remote_tail(ep: SshEndpoint, remote_path: str, lines: int = 30) -> str:
    r = ssh_exec(ep, f"tail -{lines} {shlex.quote(remote_path)} 2>/dev/null || echo '(no log)'", check=False)
    return r.stdout.strip()


def cleanup_failed_launch(ep: SshEndpoint, runtime_dir: str, launch_stdout: str) -> bool:
    """Best-effort kill of a process that may have survived a failed launch.

    A failed launch script (or unparseable PID output) can still have spawned
    the service. Returns True only when no leftover process is confirmed —
    either no PID was found, or every found PID was killed and verified dead.
    False means the process state is unknown; the caller must keep the port
    lease and report needs_repair.
    """
    candidates: set[int] = set()
    stdout_lines = (launch_stdout or "").strip().splitlines()
    if stdout_lines:
        with contextlib.suppress(ValueError):
            candidates.add(int(stdout_lines[-1].strip()))
    pid_file = f"{runtime_dir}/pid"
    pid_state = ssh_exec(ep, f"cat {shlex.quote(pid_file)} 2>/dev/null || true", check=False)
    if pid_state.returncode != 0:
        # Remote state is unreadable — cannot rule out a leftover process.
        return False
    for token in pid_state.stdout.split():
        with contextlib.suppress(ValueError):
            candidates.add(int(token))
    for pid in sorted(candidates):
        try:
            if not check_alive(ep, pid):
                continue
        except RuntimeError:
            return False
        ssh_exec(
            ep,
            f"kill -15 {pid} 2>/dev/null || true; sleep 2; kill -9 {pid} 2>/dev/null || true",
            check=False,
        )
        time.sleep(1)
        try:
            if check_alive(ep, pid):
                return False
        except RuntimeError:
            return False
    return True


def abort_failed_launch(
    *,
    ep: SshEndpoint,
    runtime_dir: str,
    launch_stdout: str,
    release_kwargs: dict[str, Any],
    payload: dict[str, Any],
) -> int:
    """Finish a failed launch attempt.

    The port lease is released only when cleanup confirms no leftover
    process; otherwise the lease is kept and the result is needs_repair so a
    human can inspect the container instead of losing track of the port.
    """
    if cleanup_failed_launch(ep, runtime_dir, launch_stdout):
        release_service_port(**release_kwargs)
        payload["status"] = "failed"
    else:
        payload["status"] = "needs_repair"
        payload["error"] = (
            f"{payload['error']}; a leftover process could not be confirmed "
            "dead, so the service port lease was kept — inspect the container, "
            "kill any leftover vllm process, then release the port lease"
        )
    print_json(payload)
    return 1


# ---------------------------------------------------------------------------
# Environment error diagnosis
# ---------------------------------------------------------------------------

_ENV_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("Failed to infer device type", "device-type"),
    ("No module named 'vllm_ascend'", "missing-vllm-ascend"),
    ("No module named 'vllm'", "missing-vllm"),
    ("No module named 'torch_npu'", "missing-torch-npu"),
    ("cannot open shared object file", "missing-so"),
    ("libhccl.so", "missing-so"),
    ("RuntimeError:.*torch_npu", "torch-npu-error"),
    ("ImportError", "import-error"),
    ("ModuleNotFoundError", "module-not-found"),
]


def diagnose_env_failure(
    stderr_tail: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Scan stderr for environment-related errors and return structured recovery guidance.

    Returns a dict with diagnosis details, or None if the error
    doesn't look environment-related.
    """
    if not stderr_tail:
        return None

    matched_tags: list[str] = []
    for pattern, tag in _ENV_ERROR_PATTERNS:
        if pattern in stderr_tail or re.search(pattern, stderr_tail):
            matched_tags.append(tag)

    if not matched_tags:
        return None

    recovery_parts = ["python3 .agents/skills/remote-code-parity/scripts/parity_sync.py"]
    if session_id:
        recovery_parts.append(f"--session-id {session_id}")
    recovery_parts.append("--force-reinstall")
    return {
        "error_tags": sorted(set(matched_tags)),
        "cause": "remote Python package version mismatch",
        "recovery_command": " ".join(recovery_parts),
        "recovery_description": (
            "Re-run parity sync with --force-reinstall to rebuild "
            "vllm and vllm-ascend in the correct order with pinned dependencies."
        ),
        "warning": (
            "Do NOT run bare `pip install` inside the container. "
            "The container has exact version locks between torch, torch_npu, "
            "vllm, and vllm-ascend. Manual pip install will break the "
            "dependency graph. Parity sync uses the correct install flags "
            "(--no-deps, --no-build-isolation, VLLM_TARGET_DEVICE=empty, HuaweiCloud pip index)."
        ),
    }


_STAGE_MARKERS: list[tuple[str, str]] = [
    ("uvicorn running", "http-up"),
    ("application startup complete", "http-up"),
    ("capturing", "graph-capture"),
    ("graph capture", "graph-capture"),
    ("aclgraph", "graph-capture"),
    ("acl graph", "graph-capture"),
    ("torch.compile", "compile"),
    ("loading weights", "weight-load"),
    ("loading safetensors", "weight-load"),
    ("model loading", "weight-load"),
]

_STAGE_GREP = "loading weights|loading safetensors|model loading|torch\\.compile|capturing|aclgraph|acl graph|graph capture|uvicorn running|application startup complete"


def classify_stage(text: str) -> str | None:
    """Map a runtime log line to a startup phase, or None when unrecognized."""
    lowered = text.lower()
    for needle, stage in _STAGE_MARKERS:
        if needle in lowered:
            return stage
    return None


def probe_ready_once(
    ep: SshEndpoint,
    pid: int,
    port: int,
    *,
    runtime_dir: str | None = None,
) -> dict[str, Any]:
    """Alive + /health + /v1/models (+ startup stage) in a single SSH round-trip.

    The readiness loop used to issue up to three separate SSH commands per
    tick; over a long model load that is hundreds of avoidable round-trips.
    A failed SSH round-trip is ``probe_error`` (liveness unknown), never proof
    of process exit.
    """
    lines = [
        f"if kill -0 {pid} 2>/dev/null; then echo __ALIVE__=1; else echo __ALIVE__=0; fi",
        (
            f"code=$(curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3 --max-time 5 "
            f"http://127.0.0.1:{port}/health 2>/dev/null || echo 000)"
        ),
        'echo "__HEALTH__=$code"',
        'if [ "$code" = "200" ]; then',
        "  echo __MODELS_BEGIN__",
        f"  curl -s --connect-timeout 3 --max-time 5 http://127.0.0.1:{port}/v1/models 2>/dev/null",
        "  echo",
        "  echo __MODELS_END__",
        "fi",
    ]
    if runtime_dir:
        lines += [
            "echo __STAGE_BEGIN__",
            (
                f"tail -n 300 {shlex.quote(runtime_dir)}/stdout.log {shlex.quote(runtime_dir)}/stderr.log 2>/dev/null "
                f"| grep -iE '{_STAGE_GREP}' | tail -1 | head -c 160"
            ),
            "echo",
            "echo __STAGE_END__",
        ]
    r = ssh_exec(ep, "\n".join(lines), check=False)
    out = r.stdout or ""
    # Any nonzero rc means the round-trip did not complete cleanly; even an
    # __ALIVE__=0 marker in torn output is not proof of process exit.
    probe_error = r.returncode != 0
    alive = "__ALIVE__=1" in out
    health_ok = "__HEALTH__=200" in out
    models: dict[str, Any] | None = None
    if "__MODELS_BEGIN__" in out and "__MODELS_END__" in out:
        body = out.split("__MODELS_BEGIN__", 1)[1].split("__MODELS_END__", 1)[0].strip()
        try:
            data = json.loads(body)
            if data.get("data"):
                models = data
        except json.JSONDecodeError:
            models = None
    stage = None
    if "__STAGE_BEGIN__" in out and "__STAGE_END__" in out:
        marker = out.split("__STAGE_BEGIN__", 1)[1].split("__STAGE_END__", 1)[0].strip()
        stage = classify_stage(marker)
    return {"alive": alive, "health": health_ok, "models": models,
            "stage": stage, "probe_error": probe_error}


def probe_first_token(ep: SshEndpoint, port: int, served_model: str) -> dict[str, Any]:
    """One deterministic real request; /health 200 alone is not a ready service."""
    payload = json.dumps({"model": served_model, "prompt": "Hello", "max_tokens": 8, "temperature": 0})
    # $$ is the remote shell PID, so concurrent probes never share a body file.
    script = (
        "tmp=/tmp/vaws_first_token.$$.json; "
        f"code=$(curl -s -o $tmp -w '%{{http_code}}' --connect-timeout 3 --max-time 120 "
        f"-X POST http://127.0.0.1:{port}/v1/completions -H 'Content-Type: application/json' "
        f"-d {shlex.quote(payload)} 2>/dev/null || echo 000); "
        "echo __CODE__=$code; head -c 400 $tmp 2>/dev/null; rm -f $tmp"
    )
    r = ssh_exec(ep, script, check=False)
    out = r.stdout or ""
    return {
        "ok": "__CODE__=200" in out,
        # Any nonzero rc is an unknown probe result, not proof of failure.
        "probe_error": r.returncode != 0,
        "detail": out[-300:],
    }


def wait_for_ready(
    ep: SshEndpoint,
    pid: int,
    port: int,
    runtime_dir: str,
    timeout: int,
    served_model: str,
) -> dict[str, Any]:
    start = time.monotonic()
    deadline = start + timeout
    health_ok = False
    models_ok = False
    token_ok = False
    phases: list[dict[str, Any]] = []

    def mark(stage: str) -> None:
        if not phases or phases[-1]["phase"] != stage:
            phases.append({"phase": stage, "at_seconds": round(time.monotonic() - start, 1)})
            emit_progress("probe", f"phase: {stage}")

    def last_phase() -> str:
        return phases[-1]["phase"] if phases else "launch"

    while time.monotonic() < deadline:
        probe = probe_ready_once(ep, pid, port, runtime_dir=runtime_dir)
        if probe.get("probe_error"):
            # A lost SSH round-trip is not a dead process; keep waiting.
            time.sleep(HEALTH_POLL_INTERVAL)
            continue
        if not probe["alive"]:
            stderr_tail = read_remote_tail(ep, f"{runtime_dir}/stderr.log")
            return {
                "ready": False,
                "alive": False,
                "error": "process exited before becoming ready",
                "stderr_tail": stderr_tail,
                "phases": phases,
                "last_phase": last_phase(),
                "elapsed_seconds": round(time.monotonic() - start, 1),
            }

        if probe.get("stage"):
            mark(probe["stage"])

        if not health_ok and probe["health"]:
            health_ok = True
            mark("health-ok")

        if health_ok and not models_ok and probe["models"] is not None:
            models_ok = True
            mark("models-ok")

        if models_ok and not token_ok:
            token = probe_first_token(ep, port, served_model)
            if token.get("probe_error"):
                time.sleep(HEALTH_POLL_INTERVAL)
                continue
            if token["ok"]:
                token_ok = True
                mark("first-token-ok")
            else:
                mark("first-token-failing")

        if health_ok and models_ok and token_ok:
            return {
                "ready": True,
                "alive": True,
                "phases": phases,
                "elapsed_seconds": round(time.monotonic() - start, 1),
            }

        time.sleep(HEALTH_POLL_INTERVAL)

    return {
        "ready": False,
        "alive": check_alive(ep, pid),
        "health": health_ok,
        "models": models_ok,
        "first_token": token_ok,
        "error": (
            f"timed out after {timeout}s waiting for service "
            f"(last phase: {last_phase()}); raise --health-timeout if this stage legitimately takes longer"
        ),
        "phases": phases,
        "last_phase": last_phase(),
        "elapsed_seconds": round(time.monotonic() - start, 1),
    }


# ---------------------------------------------------------------------------
# Preset preflight
# ---------------------------------------------------------------------------

_JSON_VALUE_FLAGS = (
    "--additional-config",
    "--model-loader-extra-config",
    "--speculative-config",
    "--compilation-config",
)


def preflight_preset(
    ep: SshEndpoint,
    preset: dict[str, Any],
    *,
    runtime_base: str,
    env: dict[str, str],
    extra_args: list[str],
) -> list[str]:
    """Verify a preset against the actual container before touching any service.

    Recipe/version drift is the most common launch-failure class (stale arg
    types, CANN path moves, unverified vllm). Fail here, before a multi-minute
    model load is wasted on it — and before any running service is stopped.
    """
    problems: list[str] = []
    expected_vllm = str(preset.get("vllm_version") or "")
    if expected_vllm:
        version_file = f"{runtime_base}/vllm/vllm/_version.py"
        r = ssh_exec(ep, f"grep -m1 '^__version__ = ' {shlex.quote(version_file)} 2>/dev/null || true", check=False)
        match = re.search(r"'([0-9][^']*)'", r.stdout or "")
        actual_vllm = match.group(1) if match else ""
        if actual_vllm != expected_vllm:
            problems.append(
                f"preset is verified for vllm {expected_vllm}, but the container has "
                f"{actual_vllm or 'an unreadable vllm version'}; use a preset verified for this image"
            )
    missing: list[str] = []
    for entry in (env.get("PYTHONPATH") or "").split(":"):
        entry = entry.strip()
        if entry.startswith("/"):
            r = ssh_exec(ep, f"test -d {shlex.quote(entry)}", check=False)
            if r.returncode != 0:
                missing.append(entry)
    if missing:
        problems.append("PYTHONPATH entries missing in the container: " + ", ".join(missing))
    for flag in _JSON_VALUE_FLAGS:
        # Validate every occurrence, in both "--flag value" and "--flag=value"
        # forms — a bad second occurrence must not slip through.
        for idx, arg in enumerate(extra_args):
            if arg == flag:
                if idx + 1 >= len(extra_args):
                    problems.append(f"{flag} has no value")
                    continue
                value = extra_args[idx + 1]
            elif arg.startswith(f"{flag}="):
                value = arg[len(flag) + 1:]
            else:
                continue
            try:
                json.loads(value)
            except json.JSONDecodeError as exc:
                problems.append(f"{flag} value is not valid JSON: {exc}")
    return problems


# ---------------------------------------------------------------------------
# Relaunch merge
# ---------------------------------------------------------------------------

def merge_with_previous(
    previous: dict[str, Any],
    *,
    model: str | None,
    served_model_name: str | None,
    tp: int | None,
    dp: int | None,
    devices: str | None,
    extra_env: dict[str, str],
    unset_env: list[str],
    extra_args: list[str],
    unset_args: list[str],
) -> dict[str, Any]:
    merged = dict(previous)
    if model is not None:
        merged["model"] = model
    if served_model_name is not None:
        merged["served_model_name"] = served_model_name
    if tp is not None:
        merged["tp"] = tp
    if dp is not None:
        merged["dp"] = dp
    if devices is not None:
        merged["devices"] = devices

    prev_env = dict(merged.get("env", {}))
    for key in unset_env:
        prev_env.pop(key, None)
    prev_env.update(extra_env)
    merged["env"] = prev_env

    prev_args = list(merged.get("extra_args", []))
    if unset_args:
        cleaned: list[str] = []
        i = 0
        while i < len(prev_args):
            arg = prev_args[i]
            if any(arg.startswith(u) for u in unset_args):
                if "=" not in arg:
                    nxt = prev_args[i + 1] if i + 1 < len(prev_args) else None
                    if nxt is not None and not nxt.startswith("-"):
                        i += 1
                i += 1
                continue
            cleaned.append(arg)
            i += 1
        prev_args = cleaned
    prev_args.extend(extra_args)
    merged["extra_args"] = prev_args

    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    p.add_argument("--session-id", help="VAWS session id; defaults to the bound session of the current worktree")
    p.add_argument("--session-file", help="explicit session.json path")
    p.add_argument("--preset", help="named serving preset from the skill's presets/ directory; CLI args override preset values")
    p.add_argument("--model", help="absolute model weight path on the remote container")
    p.add_argument(
        "--served-model-name", "--served-name",
        dest="served_model_name",
        help="model name exposed via /v1/models (default: directory basename of --model)",
    )
    p.add_argument("--tp", "--tensor-parallel-size", dest="tp", type=int)
    p.add_argument("--dp", "--data-parallel-size", dest="dp", type=int)
    p.add_argument("--devices", help="ASCEND_RT_VISIBLE_DEVICES, e.g. 0,1,2,3")
    p.add_argument(
        "--extra-env", action="append", default=[],
        help="KEY=VALUE (repeatable)",
    )
    p.add_argument(
        "--unset-env", action="append", default=[],
        help="remove an env var from inherited config (repeatable)",
    )
    p.add_argument(
        "--unset-args", action="append", default=[],
        help="remove a vllm arg prefix from inherited config (repeatable)",
    )
    p.add_argument("--relaunch", action="store_true", help="reuse previous config as base")
    p.add_argument("--skip-parity", action="store_true", help="skip remote-code-parity gate")
    p.add_argument("--port", type=int, help="force a specific port")
    p.add_argument(
        "--health-timeout", type=int, default=DEFAULT_HEALTH_TIMEOUT,
        help=f"seconds to wait for /health + /v1/models (default: {DEFAULT_HEALTH_TIMEOUT})",
    )
    p.add_argument(
        "--wrap-script", default="",
        help="remote path to a wrapper script that receives the serve script path "
        "and runtime dir as $1 and $2. The wrapper controls how the service is launched "
        "(e.g. msprof wrapping). The serving skill is agnostic to what the wrapper does.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Split on bare -- to separate our args from vllm passthrough args
    own_argv: list[str] = argv
    vllm_extra: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        own_argv = argv[:idx]
        vllm_extra = argv[idx + 1:]

    args = build_parser().parse_args(own_argv)

    # ---- preset: named recipe as defaults under explicit CLI args ----
    preset: dict[str, Any] | None = None
    if args.preset:
        preset = load_preset(args.preset)
        if args.tp is None and preset.get("tp") is not None:
            args.tp = int(preset["tp"])
        if args.dp is None and preset.get("dp") is not None:
            args.dp = int(preset["dp"])
        if args.port is None and preset.get("port") is not None:
            args.port = int(preset["port"])
        if not args.devices and preset.get("devices"):
            args.devices = str(preset["devices"])
        if not args.served_model_name and preset.get("served_model_name"):
            args.served_model_name = str(preset["served_model_name"])
        if args.health_timeout == DEFAULT_HEALTH_TIMEOUT and preset.get("health_timeout"):
            args.health_timeout = int(preset["health_timeout"])
        if not vllm_extra and preset.get("serve_args"):
            vllm_extra = [str(a) for a in preset["serve_args"]]
    lock_stack = contextlib.ExitStack()

    try:
        # ---- resolve target ----
        target_label = args.session_id or args.session_file or "bound session"
        emit_progress("resolve-target", f"looking up {target_label}")
        target = resolve_execution_target(
            session_id=args.session_id,
            session_file=args.session_file,
        )
        record = target.record
        alias = target.alias
        ep = target.endpoint
        runtime_base = target.runtime_base
        emit_progress("lock", f"acquiring serving lock for session {target.session_id}")
        lock_stack.enter_context(
            file_lock(session_lock_dir(target.state_repo_root) / f"{target.session_id}.serving.lock")
        )

        # ---- parse env overrides ----
        extra_env: dict[str, str] = {}
        for item in args.extra_env:
            if "=" not in item:
                print_json({"status": "failed", "error": f"bad --extra-env {item!r}, expected KEY=VALUE"})
                return 1
            k, _, v = item.partition("=")
            try:
                extra_env[require_env_name(k.strip())] = v
            except ValueError as exc:
                print_json({"status": "needs_input", "error": str(exc)})
                return 1
        if preset and preset.get("env"):
            preset_env: dict[str, str] = {}
            for key, value in (preset["env"] or {}).items():
                try:
                    preset_env[require_env_name(str(key))] = str(value)
                except ValueError as exc:
                    print_json({"status": "needs_input", "error": f"preset env: {exc}"})
                    return 1
            # CLI --extra-env wins over preset env per key.
            extra_env = {**preset_env, **extra_env}

        # ---- resolve launch params (fresh or relaunch) ----
        if args.relaunch:
            previous = load_serving_state(
                target.session_id,
                state_repo_root=target.state_repo_root,
            )
            if previous is None:
                print_json({
                    "status": "failed",
                    "error": f"no previous launch state for session {target.session_id}; cannot --relaunch without a prior start",
                    "machine": alias,
                })
                return 1
            merged = merge_with_previous(
                previous,
                model=args.model,
                served_model_name=args.served_model_name,
                tp=args.tp, dp=args.dp, devices=args.devices,
                extra_env=extra_env, unset_env=args.unset_env,
                extra_args=vllm_extra, unset_args=args.unset_args,
            )
            model = merged["model"]
            served_model_name = merged["served_model_name"]
            tp = merged.get("tp")
            dp = merged.get("dp")
            devices = merged.get("devices")
            launch_env = merged.get("env", {})
            launch_extra_args = merged.get("extra_args", [])
            emit_progress("resolve-params", "merged delta onto previous config", relaunch=True)
        else:
            if not args.model:
                print_json({
                    "status": "needs_input",
                    "error": "--model is required for a fresh start",
                    "machine": alias,
                })
                return 1
            model = args.model
            served_model_name = args.served_model_name or Path(model).name
            tp = args.tp
            dp = args.dp
            devices = args.devices
            launch_env = extra_env
            launch_extra_args = vllm_extra

        # ---- preset preflight: catch recipe/version drift before any stop/launch ----
        if preset is not None:
            problems = preflight_preset(
                ep, preset, runtime_base=runtime_base,
                env=launch_env, extra_args=launch_extra_args,
            )
            if problems:
                print_json({
                    "status": "failed",
                    "phase": "preflight",
                    "error": "preset preflight failed; the running service was left untouched",
                    "problems": problems,
                    "machine": alias,
                })
                return 1
            emit_progress("preflight", f"preset {args.preset} checks passed")

        try:
            live_devices = require_session_npu_lease(target.session, repo_root=target.state_repo_root)
        except SessionStateError as exc:
            print_json({
                "status": "needs_repair", "session_id": target.session_id,
                "error": str(exc),
            })
            return 1
        # require_session_npu_lease raises on an empty or stale lease, so
        # live_devices is always a nonempty list of ints here.
        leased = set(live_devices)
        needed_devices = tp * (dp or 1) if tp is not None else None
        if needed_devices is not None and len(leased) < needed_devices:
            print_json({
                "status": "needs_input",
                "error": (
                    f"session {target.session_id} leases {len(leased)} NPU devices "
                    f"but launch needs {needed_devices} (tp={tp}, dp={dp or 1})"
                ),
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
            })
            return 1
        if devices:
            try:
                requested = _parse_devices_csv(devices)
            except ValueError as exc:
                print_json({"status": "needs_input", "error": str(exc)})
                return 1
            if not requested.issubset(leased):
                print_json({
                    "status": "needs_input",
                    "error": (
                        f"requested devices {sorted(requested)} are outside "
                        f"session {target.session_id} lease {sorted(leased)}"
                    ),
                    "machine": alias,
                    "mode": target.mode,
                    "session_id": target.session_id,
                })
                return 1
        else:
            selected = sorted(leased)
            if needed_devices is not None:
                selected = selected[:needed_devices]
            devices = ",".join(str(item) for item in selected)
            emit_progress("lease", f"using leased session devices: {devices}")

        # Validate the new launch target before touching an existing service.
        # A mistyped model path should be a needs_input response, not a reason
        # to stop a currently running service for this machine/session.
        emit_progress("validate", f"checking model path: {model}")
        r = ssh_exec(ep, f"test -d {shlex.quote(model)} || test -f {shlex.quote(model)}", check=False)
        if r.returncode != 0:
            print_json({
                "status": "needs_input",
                "error": f"model path not found on remote container: {model}",
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
            })
            return 1

        # ---- stop existing service for this session ----
        prev_state = load_serving_state(
            target.session_id,
            state_repo_root=target.state_repo_root,
        )
        if prev_state and prev_state.get("pid"):
            old_pid = prev_state["pid"]
            scope = f"session {target.session_id}"
            if not check_alive(ep, int(old_pid)):
                emit_progress("stop-existing", f"previous service for {scope} is already stopped (pid={old_pid})")
                prev_state["status"] = "stopped"
                prev_state["stopped_at"] = now_utc()
                save_serving_state(
                    target.session_id,
                    prev_state,
                    state_repo_root=target.state_repo_root,
                )
                release_service_port(
                    repo_root=target.state_repo_root,
                    machine_alias=alias,
                    session_id=target.session_id,
                    port=prev_state.get("port"),
                )
            else:
                emit_progress("stop-existing", f"stopping previous service for {scope} (pid={old_pid})")
                ssh_exec(
                    ep,
                    f"kill -2 {old_pid} 2>/dev/null || true; sleep 2; kill -15 {old_pid} 2>/dev/null || true",
                    check=False,
                )
                deadline = time.time() + 20
                while check_alive(ep, int(old_pid)) and time.time() < deadline:
                    time.sleep(1)
                if check_alive(ep, int(old_pid)):
                    emit_progress("stop-existing", f"previous service still alive, sending SIGKILL to pid={old_pid}")
                    ssh_exec(ep, f"kill -9 {old_pid} 2>/dev/null || true", check=False)
                    time.sleep(2)
                old_devices = _parse_devices_csv(str(prev_state.get("devices") or ""))
                if old_devices:
                    emit_progress("stop-existing", f"waiting for old service devices to free: {sorted(old_devices)}")
                    if not wait_for_devices_free(target.host_endpoint, old_devices):
                        # Not fatal: the probe-npus gate below re-validates
                        # occupancy before launch, but make the uncertainty
                        # visible instead of dropping it.
                        emit_progress(
                            "stop-existing",
                            f"old service devices not confirmed free after wait: {sorted(old_devices)}",
                            warning="devices-may-still-be-busy",
                        )
                if not check_alive(ep, int(old_pid)):
                    prev_state["status"] = "stopped"
                    prev_state["stopped_at"] = now_utc()
                    save_serving_state(
                        target.session_id,
                        prev_state,
                        state_repo_root=target.state_repo_root,
                    )
                    release_service_port(
                        repo_root=target.state_repo_root,
                        machine_alias=alias,
                        session_id=target.session_id,
                        port=prev_state.get("port"),
                    )
                else:
                    # The previous API server survived SIGINT+SIGTERM+SIGKILL.
                    # Launching now would create a second instance fighting for
                    # the same port/NPU devices, so fail fast instead of masking
                    # it by recording "stopping" and continuing.
                    prev_state["status"] = "stopping"
                    prev_state["status_checked_at"] = now_utc()
                    save_serving_state(
                        target.session_id,
                        prev_state,
                        state_repo_root=target.state_repo_root,
                    )
                    print_json({
                        "status": "failed",
                        "error": (
                            f"previous service pid={old_pid} did not exit after "
                            "SIGINT+SIGTERM+SIGKILL; refusing to launch a second "
                            "instance. Investigate the stuck process, then retry."
                        ),
                        "machine": alias,
                        "mode": target.mode,
                        "session_id": target.session_id,
                        "previous_pid": old_pid,
                    })
                    return 1

        # ---- parity gate ----
        if not args.skip_parity:
            emit_progress("parity-sync", "ensuring remote code parity")
            parity = run_parity(target.session_id, target.session_file)
            parity_status = parity.get("status")
            # `auto` returns `materialized` after Python-only runtime updates.
            # `source-only` only updates the cache, not the execution tree;
            # neither that nor a dry run authorizes serving the new snapshot.
            if parity_status not in ("ready", "ok", "success", "skipped", "materialized"):
                print_json({
                    "status": "blocked",
                    "error": f"remote-code-parity did not return a ready state (got {parity_status!r})",
                    "parity": parity,
                    "machine": alias,
                })
                return 1
            emit_progress("parity-sync", "parity confirmed")
        else:
            parity = {"status": "skipped"}

        # ---- probe NPUs on the HOST for cross-container visibility ----
        h_ep = target.host_endpoint
        emit_progress("probe-npus", "checking NPU device availability (host)")
        try:
            npu_info = probe_npus(h_ep)
        except RuntimeError as exc:
            print_json({
                "status": "blocked",
                "phase": "probe-npus",
                "error": (
                    "host NPU occupancy could not be verified; refusing to launch "
                    f"because session leases do not exclude unmanaged workloads: {exc}"
                ),
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
            })
            return 1

        try:
            resolved_devices, device_error = select_devices(
                npu_info, requested_devices=devices, tp=tp, dp=dp,
            )
        except ValueError as exc:
            print_json({"status": "needs_input", "error": str(exc), "npu_info": npu_info})
            return 1
        if device_error:
            print_json({
                "status": "needs_input",
                "error": device_error,
                "npu_info": npu_info,
                "machine": alias,
            })
            return 1
        if resolved_devices is not None:
            devices = resolved_devices
            emit_progress(
                "probe-npus",
                f"using devices: {devices}",
                free=npu_info.get("free"),
                busy=list(npu_info.get("busy", {}).keys()),
            )

        # ---- port ----
        emit_progress("allocate-port", "allocating session service port")
        port_available = remote_port_availability(ep)
        port = allocate_service_port(
            repo_root=target.state_repo_root,
            machine_alias=alias,
            session_id=target.session_id,
            requested_port=args.port,
            port_available=port_available,
        )
        if not remote_port_available(ep, port):
            release_service_port(
                repo_root=target.state_repo_root,
                machine_alias=alias,
                session_id=target.session_id,
                port=port,
            )
            print_json({
                "status": "failed",
                "error": f"allocated service port {port} became unavailable before launch",
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
            })
            return 1
        emit_progress("allocate-port", f"port {port}", port=port)

        # ---- launch ----
        workspace_identity = load_workspace_identity()
        unified_alias = effective_workspace_alias()
        launch_env = dict(launch_env)
        if workspace_identity is not None:
            launch_env["VAWS_AGENT_ID"] = workspace_identity["agent_id"]
        if unified_alias:
            launch_env["VAWS_AGENT_ALIAS"] = unified_alias
            launch_env["VAWS_PROJECT_ALIAS"] = unified_alias
        else:
            launch_env.pop("VAWS_AGENT_ALIAS", None)
            launch_env.pop("VAWS_PROJECT_ALIAS", None)
        instance_ts = now_utc().replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
        runtime_dir = service_runtime_dir(runtime_base, instance_ts, unified_alias)

        wrap_script = getattr(args, "wrap_script", "") or ""
        if wrap_script:
            emit_progress("launch", f"starting vllm serve (wrapped by {wrap_script})")
        else:
            emit_progress("launch", "starting vllm serve")
        port_release = {
            "repo_root": target.state_repo_root,
            "machine_alias": alias,
            "session_id": target.session_id,
            "port": port,
        }
        try:
            script = build_launch_script(
                runtime_dir=runtime_dir,
                model=model,
                served_model_name=served_model_name,
                port=port,
                tp=tp, dp=dp,
                devices=devices,
                extra_env=launch_env,
                extra_args=launch_extra_args,
                wrap_script=wrap_script,
            )
        except ValueError as exc:
            # Rejected before anything ran remotely — safe to release the port.
            release_service_port(**port_release)
            print_json({
                "status": "needs_input",
                "error": str(exc),
                "machine": alias,
                "mode": target.mode,
                "session_id": target.session_id,
            })
            return 1
        result = ssh_exec(ep, script, check=False)
        if result.returncode != 0:
            return abort_failed_launch(
                ep=ep,
                runtime_dir=runtime_dir,
                launch_stdout=result.stdout,
                release_kwargs=port_release,
                payload={
                    "error": "launch script failed",
                    "stderr_tail": result.stderr[-1000:],
                    "stdout_tail": result.stdout[-500:],
                    "machine": alias,
                },
            )

        pid_line = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        try:
            pid = int(pid_line)
        except ValueError:
            return abort_failed_launch(
                ep=ep,
                runtime_dir=runtime_dir,
                launch_stdout=result.stdout,
                release_kwargs=port_release,
                payload={
                    "error": f"cannot parse PID from launch output: {pid_line!r}",
                    "machine": alias,
                },
            )

        emit_progress("launch", f"process started pid={pid}", pid=pid)

        state = {
            "model": model,
            "served_model_name": served_model_name,
            "tp": tp,
            "dp": dp,
            "devices": devices,
            "env": launch_env,
            "extra_args": launch_extra_args,
            "machine": alias,
            "mode": target.mode,
            "session_id": target.session_id,
            "pid": pid,
            "port": port,
            "base_url": f"http://{ep.host}:{port}",
            "runtime_dir": runtime_dir,
            "log_stdout": f"{runtime_dir}/stdout.log",
            "log_stderr": f"{runtime_dir}/stderr.log",
            "started_at": now_utc(),
            "status": "starting",
            "agent_id": workspace_identity.get("agent_id") if workspace_identity else None,
            "agent_alias": unified_alias,
            "project_alias": unified_alias,
        }
        if wrap_script:
            state["wrap_script"] = wrap_script
        save_serving_state(
            target.session_id,
            state,
            state_repo_root=target.state_repo_root,
        )

        # ---- probe readiness ----
        emit_progress("probe", f"waiting for ready (timeout={args.health_timeout}s)")
        readiness = wait_for_ready(ep, pid, port, runtime_dir, timeout=args.health_timeout,
                                   served_model=served_model_name)

        # ---- persist state (always, even if not ready — so stop can clean up) ----
        state["status"] = "ready" if readiness["ready"] else "started"
        state["readiness_checked_at"] = now_utc()
        save_serving_state(
            target.session_id,
            state,
            state_repo_root=target.state_repo_root,
        )

        # ---- build output ----
        output: dict[str, Any] = {
            "status": "ready" if readiness["ready"] else "failed",
            "machine": alias,
            "mode": target.mode,
            "session_id": target.session_id,
            "session_file": str(target.session_file) if target.session_file else None,
            "base_url": f"http://{ep.host}:{port}",
            "container_ip": ep.host,
            "port": port,
            "pid": pid,
            "served_model_name": served_model_name,
            "model": model,
            "devices": devices,
            "tp": tp,
            "dp": dp,
            "log_stdout": f"{runtime_dir}/stdout.log",
            "log_stderr": f"{runtime_dir}/stderr.log",
            "runtime_dir": runtime_dir,
            "readiness": readiness,
            "parity_status": parity.get("status"),
        }
        if launch_env:
            output["env"] = launch_env
        if launch_extra_args:
            output["extra_args"] = launch_extra_args
        if wrap_script:
            output["wrap_script"] = wrap_script
        if not readiness["ready"]:
            stderr_tail = readiness.get("stderr_tail") or read_remote_tail(ep, f"{runtime_dir}/stderr.log")
            output["stderr_tail"] = stderr_tail
            diagnosis = diagnose_env_failure(stderr_tail, session_id=target.session_id)
            if diagnosis:
                output["env_diagnosis"] = diagnosis
                emit_progress("diagnosis", diagnosis["recovery_command"],
                              error_tags=diagnosis["error_tags"])

        print_json(output)
        return 0 if readiness["ready"] else 1

    except Exception as exc:
        error_msg = str(exc)
        result: dict[str, Any] = {
            "status": "failed",
            "error": error_msg,
            "session_id": getattr(args, "session_id", None),
        }
        diagnosis = diagnose_env_failure(error_msg, session_id=getattr(args, "session_id", None))
        if diagnosis:
            result["env_diagnosis"] = diagnosis
        print_json(result)
        return 2
    finally:
        lock_stack.close()


if __name__ == "__main__":
    raise SystemExit(main())
