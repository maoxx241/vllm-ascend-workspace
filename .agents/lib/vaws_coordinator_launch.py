"""Launch the installed ``vaws-coordinator`` package with scaffold environment.

The coordinator package does not read former checkout-root environment
variables. It reads ``VAWS_PARITY_SCRIPT``,
``VAWS_PARITY_WORKSPACE_ROOT``, ``VAWS_MACHINE_INVENTORY``,
``VAWS_AGENT_SESSIONS_DIR``, and ``VAWS_HOST_QUEUE_MODULE``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_dependency import (  # noqa: E402
    REMEDY,
    USABLE_STATES,
    inspect,
)
from vaws_host_queue_module import (  # noqa: E402
    HOST_QUEUE_RELATIVE,
    HostQueueUnavailable,
    host_queue_module_path,
)
from vaws_local_state import (  # noqa: E402
    agent_sessions_root,
    shared_inventory_path,
    shared_workspace_root,
)

PACKAGE = "vaws-coordinator"
LOCAL_STATE_DIRNAME = ".vaws-local"
PARITY_SCRIPT = (
    ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts" / "remote_code_parity.py"
)

DEFAULT_ENV = {
    "VAWS_PARITY_SCRIPT": str(PARITY_SCRIPT),
    "VAWS_PARITY_WORKSPACE_ROOT": str(ROOT),
}


class CoordinatorUnavailable(RuntimeError):
    """The vaws-coordinator package is not usable in this interpreter."""


def _absolute_path(value: str, repo_root: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return str(path)


def coordinator_environment(base: Mapping[str, str] | None = None, *, repo_root: Path = ROOT) -> dict[str, str]:
    """Environment for a coordinator process (task server, CLI, hook)."""
    env = dict(os.environ if base is None else base)
    for key, value in DEFAULT_ENV.items():
        env.setdefault(key, value)
    env.setdefault("VAWS_AGENT_SESSIONS_DIR", str(agent_sessions_root(repo_root)))
    sessions = Path(env["VAWS_AGENT_SESSIONS_DIR"]).expanduser()
    if not sessions.is_absolute():
        sessions = shared_workspace_root(repo_root) / sessions
    env["VAWS_AGENT_SESSIONS_DIR"] = str(sessions)
    env.setdefault("VAWS_MACHINE_INVENTORY", str(shared_inventory_path(repo_root)))
    env["VAWS_MACHINE_INVENTORY"] = _absolute_path(env["VAWS_MACHINE_INVENTORY"], repo_root)
    if "VAWS_HOST_QUEUE_MODULE" in env:
        env["VAWS_HOST_QUEUE_MODULE"] = _absolute_path(env["VAWS_HOST_QUEUE_MODULE"], repo_root)
    env["VAWS_PARITY_SCRIPT"] = _absolute_path(env["VAWS_PARITY_SCRIPT"], repo_root)
    env["VAWS_PARITY_WORKSPACE_ROOT"] = _absolute_path(env["VAWS_PARITY_WORKSPACE_ROOT"], repo_root)
    return env


def historical_manager_state_dir(repo_root: Path = ROOT) -> Path:
    """Path the pre-extraction manager used as its default ``--state-dir``."""
    return shared_workspace_root(repo_root) / LOCAL_STATE_DIRNAME / "coordinator"


def package_status(repo_root: Path = ROOT) -> dict[str, Any]:
    """Describe the installed coordinator package."""
    info = inspect(PACKAGE, repo_root=repo_root)
    try:
        host_module = str(host_queue_module_path())
    except HostQueueUnavailable:
        host_module = HOST_QUEUE_RELATIVE
    payload: dict[str, Any] = {
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
        "host_queue_module": host_module,
        "historical_manager_state_dir": str(historical_manager_state_dir(repo_root)),
        "manager_state_dir_default": None,
    }
    return payload


def require_package(repo_root: Path = ROOT) -> dict[str, Any]:
    info = inspect(PACKAGE, repo_root=repo_root)
    if info["state"] not in USABLE_STATES:
        raise CoordinatorUnavailable(
            f"{PACKAGE} is {info['state']}; install it with `{REMEDY}`"
        )
    return info


def exec_module(module: str, args: list[str], *, repo_root: Path = ROOT) -> int:
    """Replace this process with ``python -m <module> ...`` under scaffold env."""
    require_package(repo_root)
    env = coordinator_environment(repo_root=repo_root)
    command = [sys.executable, "-m", module, *args]
    os.execve(sys.executable, command, env)
    return 0  # pragma: no cover - execve does not return
