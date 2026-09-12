"""Launch the installed ``vaws-coordinator`` package with scaffold environment.

The coordinator package does not read former checkout-root environment
variables and does not locate this tree by path. It reads
``VAWS_AGENT_SESSIONS_DIR`` and optional ``VAWS_COORDINATOR_STATE_DIR``.
"""
from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_dependency import (  # noqa: E402
    REMEDY,
    inspect,
)

PACKAGE = "vaws-coordinator"


class CoordinatorUnavailable(RuntimeError):
    """The vaws-coordinator package is not usable in this interpreter."""


def _absolute_path(value: str, repo_root: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return str(path)


def coordinator_environment(base: Mapping[str, str] | None = None, *, repo_root: Path = ROOT) -> dict[str, str]:
    """Environment for a coordinator process (task server, CLI, hook)."""
    from vaws_local_state import agent_sessions_root, shared_workspace_root

    env = dict(os.environ if base is None else base)
    if "VAWS_AGENT_SESSIONS_DIR" not in env:
        env["VAWS_AGENT_SESSIONS_DIR"] = str(agent_sessions_root(repo_root))
    sessions = Path(env["VAWS_AGENT_SESSIONS_DIR"]).expanduser()
    if not sessions.is_absolute():
        sessions = shared_workspace_root(repo_root) / sessions
    env["VAWS_AGENT_SESSIONS_DIR"] = str(sessions)
    if "VAWS_COORDINATOR_STATE_DIR" in env:
        env["VAWS_COORDINATOR_STATE_DIR"] = _absolute_path(env["VAWS_COORDINATOR_STATE_DIR"], repo_root)
    env.pop("VAWS_HOST_QUEUE_MODULE", None)
    return env


def package_status(repo_root: Path = ROOT) -> dict[str, Any]:
    """Describe the installed coordinator package."""
    from vaws_local_state import agent_sessions_root

    info = inspect(PACKAGE, repo_root=repo_root)
    return {
        "name": PACKAGE,
        "state": info["state"],
        "required_version": info.get("required_version"),
        "locked_version": info.get("locked_version"),
        "locked_commit": info.get("locked_commit"),
        "installed_version": info.get("installed_version"),
        "installed_commit": info.get("installed_commit"),
        "problems": info.get("problems"),
        "remedy": info.get("remedy"),
        "task_registry": str(agent_sessions_root(repo_root)),
    }


def require_package() -> None:
    """Check availability without rereading dependency pins during startup."""
    if find_spec("vaws_coordinator") is None:
        raise CoordinatorUnavailable(
            f"{PACKAGE} is unavailable; install it with `{REMEDY}`"
        )


def exec_module(module: str, args: list[str], *, repo_root: Path = ROOT, prepare_environment: bool = True) -> int:
    """Replace this process with ``python -m <module> ...`` under scaffold env.

    Regular launch must not write the coordinator-owned machine store. Host
    import uses ``python -m vaws_coordinator provision``.
    Explicit parser help can skip workspace environment discovery because it
    exits before reading task state or launching a service.
    """
    require_package()
    env = coordinator_environment(repo_root=repo_root) if prepare_environment else dict(os.environ)
    command = [sys.executable, "-m", module, *args]
    if os.name == "nt":
        # Windows execve spawns a replacement which outlives the MCP parent's
        # process handle. Keep stdio and termination owned by this process.
        import runpy

        os.environ.update(env)
        sys.argv = [module, *args]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0
    os.execve(sys.executable, command, env)
    return 0  # pragma: no cover - execve does not return
