"""Launch the installed ``vaws-coordinator`` package with scaffold environment.

The coordinator package does not read former checkout-root environment
variables and does not locate this tree by path. It reads
``VAWS_AGENT_SESSIONS_DIR``, optional ``VAWS_COORDINATOR_STATE_DIR``, and
optional ``VAWS_HOST_QUEUE_MODULE``. Machine records are seeded into the
coordinator's own store as a document, never as a consumer path.
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
MACHINES_FILENAME = "machines.json"


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
    env.setdefault("VAWS_AGENT_SESSIONS_DIR", str(agent_sessions_root(repo_root)))
    sessions = Path(env["VAWS_AGENT_SESSIONS_DIR"]).expanduser()
    if not sessions.is_absolute():
        sessions = shared_workspace_root(repo_root) / sessions
    env["VAWS_AGENT_SESSIONS_DIR"] = str(sessions)
    if "VAWS_HOST_QUEUE_MODULE" in env:
        env["VAWS_HOST_QUEUE_MODULE"] = _absolute_path(env["VAWS_HOST_QUEUE_MODULE"], repo_root)
    if "VAWS_COORDINATOR_STATE_DIR" in env:
        env["VAWS_COORDINATOR_STATE_DIR"] = _absolute_path(env["VAWS_COORDINATOR_STATE_DIR"], repo_root)
    return env


def seed_machine_directory(env: Mapping[str, str], *, repo_root: Path = ROOT) -> Path | None:
    """Copy the shared inventory document into the coordinator-owned store."""
    inventory = shared_inventory_path(repo_root)
    if not inventory.is_file():
        return None
    override = env.get("VAWS_COORDINATOR_STATE_DIR", "")
    if override:
        state_dir = Path(override).expanduser()
    else:
        sessions = Path(env.get("VAWS_AGENT_SESSIONS_DIR") or agent_sessions_root(repo_root))
        state_dir = sessions.expanduser().resolve().parent / "coordinator"
    dest = state_dir / MACHINES_FILENAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(inventory.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


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
    seed_machine_directory(env, repo_root=repo_root)
    command = [sys.executable, "-m", module, *args]
    os.execve(sys.executable, command, env)
    return 0  # pragma: no cover - execve does not return
