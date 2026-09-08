"""Invoke the installed vaws-remote-dev package.

Two paths are exercised on purpose:

* ``inprocess`` — the MCP dispatcher (``remote_dev.mcp.tools.call_tool``),
  the same code path an agent's MCP client takes.
* ``cli`` — ``python -m remote_dev <tool> --input-json -`` as a separate
  killable process.

Importing this package, listing operations, and replaying retained evidence
do not require the package to be importable and do not mutate the process
environment. Direct library callers of the real inprocess invoker must call
:func:`apply_real_execution_environment` once before constructing
:class:`RemoteDevInvoker` or starting endpoint workers.

Tests inject a fake invoker; nothing here is required to reach a host.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = REPO_ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_remote_dev import (  # noqa: E402
    RemoteDevUnavailable,
    require_package,
    substrate_environment,
)


@dataclass
class CliResult:
    payload: dict[str, Any] | None
    returncode: int | None
    killed: bool
    duration_ms: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    argv: list[str] = field(default_factory=list)

    @property
    def result(self) -> dict[str, Any] | None:
        if isinstance(self.payload, Mapping):
            inner = self.payload.get("result")
            return inner if isinstance(inner, Mapping) else None
        return None


class Invoker(Protocol):
    def call(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]: ...

    def call_cli(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        kill_after_ms: int | None = None,
        kill_mode: str = "wrapper",
        timeout_s: float = 300.0,
    ) -> CliResult: ...


def cli_tool_name(tool: str) -> str:
    """CLI verb accepted by ``python -m remote_dev`` (``probe``, ``multi-edit``)."""
    name = tool.split(".", 1)[1] if tool.startswith("remote.") else tool
    if name.endswith(".py"):
        name = name[:-3]
    if name.startswith("remote_"):
        name = name[len("remote_") :]
    if name.startswith("remote-"):
        name = name[len("remote-") :]
    return name.replace("_", "-")


def launcher_argv(tool: str, *, python: str | None = None) -> list[str]:
    """Argv for the installed package CLI; stdin is the JSON tool payload."""
    return [python or sys.executable, "-m", "remote_dev", cli_tool_name(tool), "--input-json", "-"]


def package_root() -> Path:
    """Installed ``remote_dev`` package directory from the distribution."""
    require_package()
    try:
        dist = metadata.distribution("vaws-remote-dev")
    except metadata.PackageNotFoundError as exc:
        raise RemoteDevUnavailable("vaws-remote-dev is not installed; run `uv sync`") from exc
    located = Path(dist.locate_file("remote_dev"))
    if located.is_dir():
        return located.resolve()
    init = located / "__init__.py" if located.suffix != ".py" else located
    return init.resolve().parent if init.exists() else located.resolve()


def apply_real_execution_environment(*, repo_root: Path | None = None) -> Path:
    """Fill substrate defaults in ``os.environ`` and require the package.

    Call once from the standalone real-execution entry point, and from library
    callers of the real inprocess invoker, before constructing
    :class:`RemoteDevInvoker` or starting endpoint worker threads. Caller
    resolver / runtime / state / mux values already present are kept. This does
    not write ``REMOTE_DEV_SSH_MUX``. Importing this module, ``--list``,
    retained-evidence ``--report``, and fake-invoker tests must not call this.
    """
    root = repo_root or REPO_ROOT
    os.environ.update(substrate_environment(os.environ, repo_root=root))
    return package_root()


def _module_file(module: Any) -> Path | None:
    filename = getattr(module, "__file__", None)
    if not filename:
        return None
    return Path(str(filename)).resolve()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _require_compatible_cached_module(name: str, installed_root: Path) -> None:
    module = sys.modules.get(name)
    if module is None:
        return
    origin = _module_file(module)
    if origin is None or not _is_under(origin, installed_root):
        raise RemoteDevUnavailable(
            f"incompatible {name} already imported from {origin}; "
            f"expected the installed remote_dev package at {installed_root}. "
            "Start a fresh interpreter; the harness will not evict another "
            "application's modules."
        )


def _prepare_substrate_imports() -> Path:
    """Require the installed package and refuse a colliding cached module."""
    installed = package_root()
    for name in ("remote_dev", "remote_dev.mcp", "remote_dev.mcp.tools", "remote_dev.core"):
        _require_compatible_cached_module(name, installed)
    return installed


class RemoteDevInvoker:
    """Real invoker backed by the package MCP dispatcher and CLI."""

    def __init__(self, *, python: str | None = None) -> None:
        self._python = python or sys.executable
        self._call_tool: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def _dispatcher(self) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
        with self._lock:
            if self._call_tool is None:
                installed = _prepare_substrate_imports()
                from remote_dev.mcp.tools import call_tool  # noqa: PLC0415

                origin = _module_file(sys.modules.get("remote_dev.mcp.tools"))
                if origin is None or not _is_under(origin, installed):
                    raise RemoteDevUnavailable(
                        f"remote_dev.mcp.tools loaded from {origin}, not {installed}"
                    )
                self._call_tool = call_tool
        return self._call_tool

    def call(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._dispatcher()(tool, dict(args))

    def call_cli(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        kill_after_ms: int | None = None,
        kill_mode: str = "wrapper",
        timeout_s: float = 300.0,
    ) -> CliResult:
        require_package()
        argv = launcher_argv(tool, python=self._python)
        return run_cli(argv, dict(args), kill_after_ms=kill_after_ms, kill_mode=kill_mode, timeout_s=timeout_s)


KILL_MODES = ("wrapper", "transport")


def run_cli(
    argv: list[str],
    payload: Mapping[str, Any],
    *,
    kill_after_ms: int | None = None,
    kill_mode: str = "wrapper",
    timeout_s: float = 300.0,
    cwd: Path | None = None,
) -> CliResult:
    """Run a CLI wrapper, optionally interrupting it after ``kill_after_ms``.

    ``kill_mode="wrapper"`` kills the whole process group (the agent-side
    process vanished). ``kill_mode="transport"`` kills only the wrapper's
    ``ssh`` children (the connection dropped) and lets the wrapper finish so
    its failure reporting can be judged.

    When ``kill_after_ms`` is set, the copied child environment includes
    ``REMOTE_DEV_SSH_MUX=0`` so SSH calls inside that one CLI invocation do
    not create or use the shared ControlMaster. Ordinary calls, including an
    inherited explicit override, keep the copied parent environment. This
    does not mutate ``os.environ``.
    """
    if kill_mode not in KILL_MODES:
        raise ValueError(f"kill_mode must be one of {KILL_MODES}")
    start = time.monotonic()
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    if kill_after_ms is not None:
        env["REMOTE_DEV_SSH_MUX"] = "0"
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd or REPO_ROOT),
        env=env,
        start_new_session=True,
    )
    killed = False
    stdout_b = b""
    stderr_b = b""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        if kill_after_ms is not None:
            try:
                stdout_b, stderr_b = proc.communicate(input=data, timeout=kill_after_ms / 1000)
            except subprocess.TimeoutExpired:
                killed = True
                if kill_mode == "transport":
                    _kill_ssh_children(proc.pid)
                    try:
                        stdout_b, stderr_b = proc.communicate(timeout=timeout_s)
                    except subprocess.TimeoutExpired:
                        _kill_process_group(proc)
                        stdout_b, stderr_b = _drain(proc, 30)
                else:
                    _kill_process_group(proc)
                    stdout_b, stderr_b = _drain(proc, 30)
        else:
            stdout_b, stderr_b = proc.communicate(input=data, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        stdout_b, stderr_b = _drain(proc, 30)
        killed = True
    duration_ms = int(round((time.monotonic() - start) * 1000))
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    parsed: dict[str, Any] | None = None
    # In transport kill mode the wrapper is expected to survive and still emit
    # its JSON payload; only a wrapper kill discards stdout by construction.
    if stdout.strip() and (not killed or kill_mode == "transport"):
        try:
            candidate = json.loads(stdout)
            parsed = candidate if isinstance(candidate, dict) else None
        except json.JSONDecodeError:
            parsed = None
    return CliResult(
        payload=parsed,
        returncode=proc.returncode,
        killed=killed,
        duration_ms=duration_ms,
        stdout_tail=stdout[-4000:],
        stderr_tail=stderr[-4000:],
        argv=argv,
    )


def _kill_ssh_children(pid: int) -> list[int]:
    """SIGKILL the direct ``ssh`` children of ``pid`` (simulated connection drop)."""
    killed: list[int] = []
    try:
        listing = subprocess.run(
            ["pgrep", "-P", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return killed
    for token in listing.stdout.split():
        if not token.isdigit():
            continue
        child = int(token)
        try:
            comm = subprocess.run(
                ["ps", "-o", "comm=", "-p", str(child)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if Path(comm).name != "ssh":
            continue
        try:
            os.kill(child, signal.SIGKILL)
            killed.append(child)
        except (ProcessLookupError, PermissionError):
            continue
    return killed


def _drain(proc: subprocess.Popen[bytes], timeout_s: float) -> tuple[bytes, bytes]:
    """Finish ``communicate`` after a kill without leaking TimeoutExpired."""
    try:
        return proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        return (exc.stdout or b""), (exc.stderr or b"")


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    # Kill the whole group so the ssh child dies with the wrapper: a survivor
    # would finish the transfer behind our back and hide the interruption.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
