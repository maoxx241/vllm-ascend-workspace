#!/usr/bin/env python3
"""Run the local loopback-only NPU fleet monitor through `uvx`.

vaws-top is a published Python package. `uvx` fetches and caches the pinned
release; this wrapper only launches `vaws-top serve` as a local background
process, keeps a pidfile, and probes `/api/health`. There is no checkout, no
pin file, no git, and no service manager.

The listener is always `127.0.0.1`. vaws-top observations are not allocation
authority. Coordinator execution leases remain authoritative.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = REPO_ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import STATE_DIRNAME, shared_inventory_path, shared_workspace_root  # noqa: E402

VAWS_TOP_REPO = "vllm-ascend-workspace/vaws-top"
VAWS_TOP_REF = "v0.1.0"
VAWS_TOP_SPEC = f"git+https://github.com/{VAWS_TOP_REPO}@{VAWS_TOP_REF}"
# The console script is named after the repository; derive it so the only
# literal naming the extracted project is the canonical repository identifier.
VAWS_TOP_COMMAND = VAWS_TOP_REPO.rsplit("/", 1)[-1]
UVX_PREFIX = ["uvx", "--from", VAWS_TOP_SPEC, VAWS_TOP_COMMAND]
BIND = "127.0.0.1"
DEFAULT_PORT = 8788
RUNTIME_DIRNAME = "npu-fleet-monitor"
PIDFILE_NAME = "serve.json"
LOG_NAME = "serve.log"
CONSUMER_ENV_KEYS = ("NFM_INVENTORY_FILES", "NFM_HOST_POOL_FILES", "NFM_BOOTSTRAP_COMMAND")


class MonitorError(RuntimeError):
    pass


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def runtime_dir(repo_root: Path | None = None) -> Path:
    root = shared_workspace_root(REPO_ROOT if repo_root is None else repo_root)
    return root / STATE_DIRNAME / RUNTIME_DIRNAME


def default_bootstrap_command(repo_root: Path | None = None) -> str:
    repo_root = REPO_ROOT if repo_root is None else repo_root
    script = repo_root / ".agents" / "skills" / "machine-management" / "scripts" / "manage_machine.py"
    return " ".join(
        [
            "{python}",
            shlex.quote(str(script)),
            "bootstrap-host-key",
            "--host",
            "{host}",
            "--host-port",
            "{port}",
            "--user",
            "{user}",
            "--public-key-file",
            "{public_key_file}",
            "--password-stdin",
        ]
    )


def _path_list_value(raw: str, *, label: str) -> str:
    if "\n" in raw or "\r" in raw:
        raise MonitorError(f"{label} contains a newline and will not be passed to the monitor")
    return os.pathsep.join(item.strip() for item in raw.split(os.pathsep) if item.strip())


def consumer_env(
    args: argparse.Namespace,
    *,
    repo_root: Path | None = None,
    inherited: dict[str, str] | None = None,
) -> dict[str, str]:
    """Explicit flags win, then the caller's environment, then scaffold defaults."""
    repo_root = REPO_ROOT if repo_root is None else repo_root
    inherited = dict(os.environ) if inherited is None else inherited
    shared_root = shared_workspace_root(repo_root)
    host_pool = shared_root / "hosts.txt"
    defaults = {
        "NFM_INVENTORY_FILES": str(shared_inventory_path(repo_root)),
        "NFM_HOST_POOL_FILES": str(host_pool) if host_pool.is_file() else "",
        "NFM_BOOTSTRAP_COMMAND": default_bootstrap_command(repo_root),
    }
    explicit = {
        "NFM_INVENTORY_FILES": args.inventory_files,
        "NFM_HOST_POOL_FILES": args.host_pool_files,
        "NFM_BOOTSTRAP_COMMAND": args.bootstrap_command,
    }
    env: dict[str, str] = {}
    for key in CONSUMER_ENV_KEYS:
        value = explicit[key]
        if value is None:
            value = inherited.get(key, defaults[key])
        if "\n" in value or "\r" in value:
            raise MonitorError(f"{key} contains a newline and will not be passed to the monitor")
        if key != "NFM_BOOTSTRAP_COMMAND":
            value = _path_list_value(value, label=key)
        if value:
            env[key] = value
    return env


def serve_env(args: argparse.Namespace, port: int, state_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(consumer_env(args))
    env["NFM_BIND"] = BIND
    env["NFM_PORT"] = str(port)
    env["NFM_STATE_DIR"] = str(state_dir)
    return env


def serve_command(port: int) -> list[str]:
    return [*UVX_PREFIX, "serve", "--bind", BIND, "--port", str(port)]


def require_uvx() -> str:
    path = shutil.which("uvx")
    if not path:
        raise MonitorError("uvx is not on PATH; install uv (https://docs.astral.sh/uv/) and retry")
    return path


def health_url(port: int) -> str:
    return f"http://{BIND}:{port}/api/health"


def health(port: int, wait_seconds: float = 0, *, alive: Any = None) -> tuple[bool, dict[str, Any] | None, str | None]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + wait_seconds
    error = "health endpoint unavailable"
    while True:
        try:
            with opener.open(health_url(port), timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "ok", payload, None
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
            error = str(exc)
        if time.monotonic() >= deadline or (alive is not None and not alive()):
            return False, None, error
        time.sleep(0.5)


def read_pidfile(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_tail(path: Path, lines: int = 20) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def start_process(command: list[str], *, env: dict[str, str], cwd: Path, log_path: Path) -> subprocess.Popen[bytes]:
    with log_path.open("ab") as log:
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def deploy() -> dict[str, Any]:
    """Resolve, build if needed, and cache the pinned package; no service is started."""
    uvx = require_uvx()
    progress(f"Resolving {VAWS_TOP_SPEC} through uvx (first run may build the frontend and needs Node.js)")
    result = subprocess.run(
        [*UVX_PREFIX, "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "uvx failed").strip()[-4000:]
        raise MonitorError(f"uvx could not provide {VAWS_TOP_SPEC}: {detail}")
    return {"version": result.stdout.strip(), "uvx": uvx}


def do_start(args: argparse.Namespace, base: Path) -> dict[str, Any]:
    pidfile = base / PIDFILE_NAME
    log_path = base / LOG_NAME
    existing = read_pidfile(pidfile)
    if existing and pid_alive(existing["pid"]):
        port = int(existing.get("port", args.port))
        ok, payload, error = health(port)
        return {"ok": ok, "pid": existing["pid"], "port": port, "health": payload, "health_error": error, "already_running": True}
    require_uvx()
    base.mkdir(parents=True, exist_ok=True)
    state_dir = base / "data"
    command = serve_command(args.port)
    progress(f"Starting {' '.join(shlex.quote(item) for item in command)}")
    process = start_process(command, env=serve_env(args, args.port, state_dir), cwd=base, log_path=log_path)
    record = {
        "pid": process.pid,
        "port": args.port,
        "spec": VAWS_TOP_SPEC,
        "started_at": time.time(),
        "log": str(log_path),
    }
    pidfile.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    ok, payload, error = health(args.port, args.wait_seconds, alive=lambda: process.poll() is None)
    result = {"ok": ok, "pid": process.pid, "port": args.port, "health": payload, "health_error": error, "already_running": False}
    if not ok:
        if process.poll() is not None:
            pidfile.unlink(missing_ok=True)
            result["exit_code"] = process.returncode
        result["log_tail"] = log_tail(log_path)
    return result


def do_stop(base: Path, *, timeout: float = 15) -> dict[str, Any]:
    pidfile = base / PIDFILE_NAME
    existing = read_pidfile(pidfile)
    if existing is None:
        return {"ok": True, "stopped": False, "pid": None, "detail": "no pidfile"}
    pid = existing["pid"]
    port = int(existing.get("port", DEFAULT_PORT))
    if not pid_alive(pid):
        pidfile.unlink(missing_ok=True)
        return {"ok": True, "stopped": False, "pid": pid, "port": port, "detail": "stale pidfile removed"}
    progress(f"Stopping monitor process group {pid}")
    _signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if pid_alive(pid):
        _signal_group(pid, signal.SIGKILL)
        time.sleep(0.5)
    stopped = not pid_alive(pid)
    if stopped:
        pidfile.unlink(missing_ok=True)
    return {"ok": stopped, "stopped": stopped, "pid": pid, "port": port}


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def do_status(base: Path, port: int) -> dict[str, Any]:
    existing = read_pidfile(base / PIDFILE_NAME)
    pid = existing["pid"] if existing else None
    if existing:
        port = int(existing.get("port", port))
    running = bool(pid) and pid_alive(pid)
    ok, payload, error = health(port)
    return {"ok": ok, "pid": pid if running else None, "running": running, "port": port, "health": payload, "health_error": error}


def payload_for(action: str, base: Path, port: int, extra: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "ok": False,
        "action": action,
        "allocation_authority": False,
        "repository": VAWS_TOP_REPO,
        "ref": VAWS_TOP_REF,
        "spec": VAWS_TOP_SPEC,
        "bind": BIND,
        "port": port,
        "url": f"http://{BIND}:{port}",
        "health_url": health_url(port),
        "runtime_dir": str(base),
        "state_dir": str(base / "data"),
        "log": str(base / LOG_NAME),
        "cli_prefix": UVX_PREFIX,
        "mcp_command": [*UVX_PREFIX, "mcp"],
    }
    payload.update(extra)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy, start, inspect, restart, or stop the loopback-only NPU fleet monitor through uvx"
    )
    parser.add_argument("action", choices=("deploy", "start", "status", "restart", "stop"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"loopback port (default {DEFAULT_PORT})")
    parser.add_argument("--wait-seconds", type=float, default=90, help="how long start waits for /api/health")
    parser.add_argument("--inventory-files", help="os.pathsep-separated inventory JSON files (NFM_INVENTORY_FILES)")
    parser.add_argument("--host-pool-files", help="os.pathsep-separated host pool files (NFM_HOST_POOL_FILES)")
    parser.add_argument("--bootstrap-command", help="one-time password key bootstrap template (NFM_BOOTSTRAP_COMMAND)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = runtime_dir()
    try:
        if args.port <= 0 or args.port > 65535:
            raise MonitorError(f"invalid port: {args.port}")
        if args.action == "deploy":
            extra = deploy()
            extra["ok"] = True
        elif args.action == "start":
            extra = do_start(args, base)
        elif args.action == "restart":
            stopped = do_stop(base)
            extra = do_start(args, base)
            extra["stopped_previous"] = stopped
        elif args.action == "stop":
            extra = do_stop(base)
        else:
            extra = do_status(base, args.port)
        result = payload_for(args.action, base, int(extra.get("port", args.port)), extra)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 1
    except (MonitorError, OSError) as exc:
        print(
            json.dumps(
                {"ok": False, "action": args.action, "allocation_authority": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
