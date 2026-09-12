#!/usr/bin/env python3
"""Run the local loopback-only NPU fleet monitor through `uvx`.

vaws-top is a published Python package. `uvx` fetches and caches the pinned
GitHub Release wheel (the only artifact that contains the built frontend);
this wrapper only launches `vaws-top serve` as a local background process,
keeps a pidfile, and probes `/api/health`. There is no checkout, no pin file,
no git, and no service manager.

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
import socket
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

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=REPO_ROOT)

from vaws_local_state import STATE_DIRNAME, shared_inventory_path, shared_workspace_root  # noqa: E402
from vaws_process_identity import process_identity, same_process  # noqa: E402

VAWS_TOP_REPO = "vllm-ascend-workspace/vaws-top"
VAWS_TOP_SKILL_PATH = ".agents/skills/vaws-top/SKILL.md"
# Single version constant: the release tag. The wheel filename below is derived
# from it so the tag and the wheel version cannot drift apart.
VAWS_TOP_REF = "v0.1.2"
VAWS_TOP_VERSION = VAWS_TOP_REF.removeprefix("v")
# The console script is named after the repository and the import package uses
# underscores; derive both so the only literal naming the extracted project is
# the canonical repository identifier.
VAWS_TOP_COMMAND = VAWS_TOP_REPO.rsplit("/", 1)[-1]
VAWS_TOP_PACKAGE = VAWS_TOP_COMMAND.replace("-", "_")
VAWS_TOP_WHEEL = f"{VAWS_TOP_PACKAGE}-{VAWS_TOP_VERSION}-py3-none-any.whl"
# Default install source is the GitHub Release wheel, not `git+https://...@tag`.
# vaws-top ships a JS frontend whose build output exists only in the released
# wheel. Installing from git runs a hatch hook that needs Node.js; without Node
# it silently produces a wheel with no frontend and `serve` fails on startup.
# This is specific to vaws-top; pure-Python sibling packages keep git+tag.
DEFAULT_VAWS_TOP_SPEC = f"https://github.com/{VAWS_TOP_REPO}/releases/download/{VAWS_TOP_REF}/{VAWS_TOP_WHEEL}"
SPEC_ENV = "VAWS_TOP_FROM"
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
    from vaws_coordinator.machine_directory import MACHINES_FILENAME
    from vaws_coordinator.state_paths import agent_sessions_root, coordinator_state_dir

    coordinator_inventory = coordinator_state_dir(agent_sessions_root(repo_root)) / MACHINES_FILENAME
    default_inventory = (coordinator_inventory if coordinator_inventory.is_file()
                         else shared_inventory_path(repo_root))
    defaults = {
        "NFM_INVENTORY_FILES": str(default_inventory),
        "NFM_HOST_POOL_FILES": str(host_pool) if host_pool.is_file() else "",
        "NFM_BOOTSTRAP_COMMAND": "",
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


def resolve_spec(explicit: str | None = None, inherited: dict[str, str] | None = None) -> str:
    """`--from` flag, then VAWS_TOP_FROM, then the release wheel. Developers can
    point at a local wheel, a source tree, or `git+https://...` (needs Node.js)."""
    inherited = dict(os.environ) if inherited is None else inherited
    for candidate in (explicit, inherited.get(SPEC_ENV)):
        if candidate is None:
            continue
        candidate = candidate.strip()
        if not candidate or any(char.isspace() for char in candidate):
            raise MonitorError(f"invalid monitor install spec: {candidate!r}")
        return candidate
    return DEFAULT_VAWS_TOP_SPEC


def uvx_prefix(spec: str) -> list[str]:
    return ["uvx", "--from", spec, VAWS_TOP_COMMAND]


def serve_command(spec: str, port: int) -> list[str]:
    return [*uvx_prefix(spec), "serve", "--bind", BIND, "--port", str(port)]


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
    if os.name == "nt":
        from vaws_windows import pid_alive as windows_pid_alive
        return windows_pid_alive(pid)
    try:
        if (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
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


def process_group_remains(pid: int) -> bool:
    """Observe a POSIX group without assuming a dead leader still proves ownership."""
    if os.name == "nt":
        return False  # Windows uses taskkill /T rather than POSIX process groups.
    result = subprocess.run(["ps", "-ax", "-o", "pgid=", "-o", "stat="],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        return True  # Unknown is not proof that the whole group stopped.
    return any(len(fields := line.split()) >= 2 and fields[0] == str(pid) and not fields[1].startswith("Z")
               for line in result.stdout.splitlines())


def port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((BIND, port)) == 0


def start_process(command: list[str], *, env: dict[str, str], cwd: Path, log_path: Path) -> subprocess.Popen[bytes]:
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    with log_path.open("ab") as log:
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **options,
        )


# Runs inside the installed package environment. `require_static()` is the same
# check `serve` performs at startup, so a wheel built without the frontend fails
# here instead of at the first `start`.
STATIC_PROBE = (
    f"import importlib.metadata as m; from {VAWS_TOP_PACKAGE}.static_files import require_static; "
    f"print(m.version({VAWS_TOP_COMMAND!r})); print(require_static() / 'index.html')"
)


def run_uvx(spec: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uvx", "--from", spec, *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def deploy(spec: str) -> dict[str, Any]:
    """Install/cache the package and prove the packaged frontend exists; no service is started."""
    uvx = require_uvx()
    progress(f"Resolving {spec} through uvx")
    result = run_uvx(spec, "python", "-c", STATIC_PROBE)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "uvx failed").strip()[-4000:]
        raise MonitorError(
            f"{spec} is not servable: {detail}\n"
            "The monitor needs the GitHub Release wheel (frontend built in). "
            "Installing from a source tree or git+https requires Node.js and otherwise "
            "yields a wheel without the frontend."
        )
    lines = result.stdout.strip().splitlines()
    version = lines[0] if lines else ""
    index_html = lines[1] if len(lines) > 1 else ""
    if not index_html or not Path(index_html).is_file():
        raise MonitorError(f"{spec} installed without the packaged frontend (missing {index_html or 'index.html'})")
    return {"version": version, "static_index": index_html, "uvx": uvx}


def do_start(args: argparse.Namespace, base: Path, spec: str) -> dict[str, Any]:
    pidfile = base / PIDFILE_NAME
    log_path = base / LOG_NAME
    existing = read_pidfile(pidfile)
    if existing and pid_alive(existing["pid"]):
        if not same_process(existing["pid"], existing.get("identity")):
            return {"ok": False, "pid": existing["pid"], "already_running": False,
                    "ownership": "unverified", "detail": "live PID does not match the saved process identity"}
        port = int(existing.get("port", args.port))
        ok, payload, error = health(port)
        return {
            "ok": ok,
            "pid": existing["pid"],
            "port": port,
            "spec": existing.get("spec", spec),
            "health": payload,
            "health_error": error,
            "already_running": True,
        }
    if existing and process_group_remains(existing["pid"]):
        return {"ok": False, "already_running": False, "ownership": "unverified",
                "detail": "recorded leader exited but its process group remains"}
    if port_is_listening(args.port):
        return {"ok": False, "port": args.port, "already_running": False,
                "detail": "loopback port is already listening without a matching monitor record"}
    require_uvx()
    base.mkdir(parents=True, exist_ok=True)
    state_dir = base / "data"
    command = serve_command(spec, args.port)
    progress(f"Starting {' '.join(shlex.quote(item) for item in command)}")
    process = start_process(command, env=serve_env(args, args.port, state_dir), cwd=base, log_path=log_path)
    record = {
        "pid": process.pid,
        "port": args.port,
        "spec": spec,
        "started_at": time.time(),
        "log": str(log_path),
        "identity": process_identity(process.pid),
    }
    pidfile.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    ok, payload, error = health(args.port, args.wait_seconds, alive=lambda: process.poll() is None)
    # uvx may exec the installed command during startup. Record its final command
    # only while the birth identity still belongs to the child we launched.
    current = process_identity(process.pid)
    initial = record.get("identity")
    if current and initial and current["started"] == initial["started"]:
        record["identity"] = current
        pidfile.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    owned = bool(current) and current == record.get("identity")
    result = {"ok": ok and owned, "pid": process.pid, "port": args.port, "health": payload, "health_error": error, "already_running": False,
              "ownership": "verified" if owned else "unverified"}
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
        if process_group_remains(pid):
            return {"ok": False, "stopped": False, "pid": pid, "port": port,
                    "ownership": "unverified", "group_remaining": True,
                    "detail": "recorded leader exited but its process group remains"}
        pidfile.unlink(missing_ok=True)
        return {"ok": True, "stopped": False, "pid": pid, "port": port, "detail": "stale pidfile removed"}
    if not same_process(pid, existing.get("identity")):
        return {"ok": False, "stopped": False, "pid": pid, "port": port,
                "ownership": "unverified", "detail": "live PID does not match the saved process identity"}
    progress(f"Stopping monitor process group {pid}")
    _stop_group(pid, force=False)
    deadline = time.monotonic() + timeout
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if pid_alive(pid):
        if not same_process(pid, existing.get("identity")):
            return {"ok": False, "stopped": False, "pid": pid, "port": port,
                    "ownership": "unverified", "detail": "process identity changed while stopping"}
        _stop_group(pid, force=True)
        time.sleep(0.5)
    stopped = not pid_alive(pid)
    if stopped and process_group_remains(pid):
        return {"ok": False, "stopped": False, "pid": pid, "port": port,
                "ownership": "unverified", "group_remaining": True,
                "detail": "leader exited; remaining group was not force-killed without a matching live leader"}
    if stopped:
        pidfile.unlink(missing_ok=True)
    return {"ok": stopped, "stopped": stopped, "pid": pid, "port": port}


def _stop_group(pid: int, *, force: bool) -> None:
    if os.name == "nt":
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(command, capture_output=True, timeout=10, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
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
    running = bool(pid) and same_process(pid, existing.get("identity"))
    ok, payload, error = health(port)
    result = {"ok": ok and running, "pid": pid if running else None, "running": running, "port": port, "health": payload, "health_error": error,
              "ownership": "verified" if running else "unverified"}
    if running and existing and existing.get("spec"):
        result["spec"] = existing["spec"]
    return result


def payload_for(action: str, base: Path, port: int, spec: str, extra: dict[str, Any]) -> dict[str, Any]:
    spec = str(extra.get("spec") or spec)
    prefix = uvx_prefix(spec)
    payload = {
        "ok": False,
        "action": action,
        "allocation_authority": False,
        "repository": VAWS_TOP_REPO,
        "ref": VAWS_TOP_REF,
        "spec": spec,
        "default_spec": DEFAULT_VAWS_TOP_SPEC,
        "bind": BIND,
        "port": port,
        "url": f"http://{BIND}:{port}",
        "health_url": health_url(port),
        "runtime_dir": str(base),
        "state_dir": str(base / "data"),
        "log": str(base / LOG_NAME),
        "cli_prefix": prefix,
        "mcp_command": [*prefix, "mcp"],
        "skill_url": f"https://github.com/{VAWS_TOP_REPO}/blob/{VAWS_TOP_REF}/{VAWS_TOP_SKILL_PATH}",
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
    parser.add_argument(
        "--from",
        dest="spec",
        help=f"uvx install source override (also {SPEC_ENV}); default is the release wheel",
    )
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
        spec = resolve_spec(args.spec)
        if args.action == "deploy":
            extra = deploy(spec)
            extra["ok"] = True
        elif args.action == "start":
            extra = do_start(args, base, spec)
        elif args.action == "restart":
            stopped = do_stop(base)
            extra = do_start(args, base, spec) if stopped["ok"] else dict(stopped)
            extra["stopped_previous"] = stopped
        elif args.action == "stop":
            extra = do_stop(base)
        else:
            extra = do_status(base, args.port)
        result = payload_for(args.action, base, int(extra.get("port", args.port)), spec, extra)
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
