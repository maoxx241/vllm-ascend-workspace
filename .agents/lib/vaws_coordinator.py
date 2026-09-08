"""Locate and wire the extracted vaws-coordinator, which lives in its own repository.

The coordinator owns task identity, the local task registry, the four `vaws_*`
tools, the stdio task server, the HTTP manager protocol, the runtime-pool
database and the managed-job supervisor. This module is the scaffold's side of
that contract:

* it finds the coordinator checkout (``VAWS_COORDINATOR_ROOT``, else the shared
  workspace's ``.vaws-local/vaws-coordinator``), never a path inside this tree;
* it injects the scaffold-owned host queue, machine inventory, parity script
  and the single local task-registry directory so the checkout cannot create a
  second writer or a parallel manager by guesswork;
* it exposes the dependency pin (``.agents/deps/coordinator.json``) so callers
  can report drift instead of silently running an unknown revision.

Missing, old or incompatible checkouts yield an actionable unavailable result.
Nothing here falls back to in-tree copies of the moved task-state writers.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_dependency import (  # noqa: E402
    checkout_path,
    inspect,
    load_pin,
    load_pin_file,
)
from vaws_local_state import (  # noqa: E402
    agent_sessions_root,
    shared_inventory_path,
    shared_workspace_root,
)

COORDINATOR_ROOT_ENV = "VAWS_COORDINATOR_ROOT"
DEPENDENCY_FILE = ROOT / ".agents" / "deps" / "coordinator.json"
CHECKOUT_DIRNAME = "vaws-coordinator"
LOCAL_STATE_DIRNAME = ".vaws-local"
PARITY_SCRIPT = (
    ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts" / "remote_code_parity.py"
)
HOST_QUEUE_MODULE = LIB / "vaws_npu_coordination.py"

REQUIRED_FILES = (
    "task_server.py",
    "scripts/vaws.py",
    "hooks/vaws_session.py",
    "lib/vaws_ops.py",
    "lib/vaws_agent_session.py",
    "lib/vaws_task_client.py",
    "workers/managed_jobs.py",
    "server.py",
)

# Environment keys this module owns defaults for. Caller/client values win.
# There is deliberately no default manager --state-dir: guessing one would fork
# the runtime-pool database. Operators pass --state-dir to server.py.
DEFAULT_ENV = {
    "VAWS_HOST_QUEUE_MODULE": str(HOST_QUEUE_MODULE),
    "VAWS_PARITY_SCRIPT": str(PARITY_SCRIPT),
    "VAWS_PARITY_WORKSPACE_ROOT": str(ROOT),
}


class CoordinatorUnavailable(RuntimeError):
    """No usable vaws-coordinator checkout is configured."""


def _flatten_pin(pin: dict[str, Any]) -> dict[str, Any]:
    """Expose coordinator extensions at the top level for existing callers."""
    data = dict(pin)
    for key, value in (pin.get("extensions") or {}).items():
        data.setdefault(key, value)
    return data


def load_dependency(path: Path = DEPENDENCY_FILE) -> dict[str, Any]:
    """Return the tracked dependency pin (repository, ref, commit)."""
    if path == DEPENDENCY_FILE:
        return _flatten_pin(load_pin("vaws-coordinator"))
    return _flatten_pin(load_pin_file(path))


def default_checkout_dir(repo_root: Path = ROOT) -> Path:
    """Where `vaws.py bootstrap` clones the coordinator by default.

    One checkout per shared workspace, next to the machine inventory, so every
    linked session worktree reaches the same revision.
    """
    path, _source = checkout_path("vaws-coordinator", env={}, repo_root=repo_root)
    return path


def looks_like_checkout(path: Path) -> bool:
    return all((path / relative).is_file() for relative in REQUIRED_FILES)


def _configured_root(env: Mapping[str, str]) -> tuple[Path | None, str]:
    path, source = checkout_path("vaws-coordinator", env=env)
    return path, source


def coordinator_root(*, required: bool = True, env: Mapping[str, str] | None = None) -> Path | None:
    """Resolve the coordinator checkout root.

    Order: ``VAWS_COORDINATOR_ROOT``, then the default checkout directory. A
    configured path that does not look like a checkout is an error either way;
    a missing default is an error only when ``required``. Drift (``off_pin``)
    is not an execution gate.
    """
    env = os.environ if env is None else env
    info = inspect("vaws-coordinator", env)
    if info["state"] in {"ready", "off_pin"}:
        return Path(info["path"]).expanduser().resolve()
    if not required:
        return None
    pin = load_pin("vaws-coordinator")
    hint = f"clone it with `{pin['bootstrap']}` or set {pin['root_env']}"
    candidate = Path(info["path"])
    if info["source"] == "env":
        raise CoordinatorUnavailable(
            f"{COORDINATOR_ROOT_ENV}={candidate} is not a vaws-coordinator checkout "
            f"(state={info['state']}); {hint}"
        )
    raise CoordinatorUnavailable(f"vaws-coordinator checkout not found at {candidate}; {hint}")


def add_coordinator_to_path(root: Path | None = None, *, prepend: bool = True) -> Path:
    """Make coordinator ``lib/`` importable from the checkout.

    Prepends by default so a leftover in-tree module cannot shadow the
    extracted writer. The coordinator also ships ``lib/vendor``.
    """
    resolved = (root or coordinator_root()).resolve()
    for relative in ("lib", "lib/vendor"):
        entry = str(resolved / relative)
        if entry not in sys.path:
            if prepend:
                sys.path.insert(0, entry)
            else:
                sys.path.append(entry)
    return resolved


def historical_manager_state_dir(repo_root: Path = ROOT) -> Path:
    """Path the pre-extraction manager used as its default ``--state-dir``.

    Documented so an existing pool database is reused. Never used as a
    heuristic default by this locator or the launcher.
    """
    return shared_workspace_root(repo_root) / LOCAL_STATE_DIRNAME / "coordinator"


def _absolute_path(value: str, repo_root: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return str(path)


def coordinator_environment(base: Mapping[str, str] | None = None, *, repo_root: Path = ROOT) -> dict[str, str]:
    """Environment for a coordinator process (task server, CLI, hook).

    Starts from ``base`` (default: the current process environment) and fills
    the keys the checkout needs to share this scaffold's host queue, inventory,
    parity script and one task registry. Values already present are kept.
    Remote-dev is optional: local attach/finish must work without it.
    """
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
    env["VAWS_HOST_QUEUE_MODULE"] = _absolute_path(env["VAWS_HOST_QUEUE_MODULE"], repo_root)
    env["VAWS_PARITY_SCRIPT"] = _absolute_path(env["VAWS_PARITY_SCRIPT"], repo_root)
    env["VAWS_PARITY_WORKSPACE_ROOT"] = _absolute_path(env["VAWS_PARITY_WORKSPACE_ROOT"], repo_root)
    try:
        from vaws_remote_dev import remote_dev_root
    except ImportError:
        remote_dev_root = None  # pragma: no cover - locator always ships with the substrate helper
    if remote_dev_root is not None:
        remote = remote_dev_root(required=False, env=env)
        if remote is not None:
            env.setdefault("VAWS_REMOTE_DEV_ROOT", str(remote))
    return env


def checkout_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def checkout_status(env: Mapping[str, str] | None = None, *, repo_root: Path = ROOT) -> dict[str, Any]:
    """Describe the configured checkout without importing it."""
    env = os.environ if env is None else env
    candidate, source = _configured_root(env)
    pin = load_dependency()
    payload: dict[str, Any] = {
        "root": str(candidate) if candidate else None,
        "root_source": source,
        "root_env": COORDINATOR_ROOT_ENV,
        "repository": pin.get("repository"),
        "pinned_ref": pin.get("ref"),
        "pinned_commit": pin.get("commit"),
        "task_registry": str(agent_sessions_root(repo_root)),
        "host_queue_module": str(HOST_QUEUE_MODULE),
        "historical_manager_state_dir": str(historical_manager_state_dir(repo_root)),
        "manager_state_dir_default": None,
    }
    info = inspect("vaws-coordinator", env, repo_root=repo_root)
    payload.update(
        root=info["path"],
        root_source=info["source"],
        state=info["state"],
        commit=info["commit"],
        pin_matches=info["pin_matches"],
        origin_matches=info["origin_matches"],
        problems=info["problems"],
    )
    if info["state"] == "not_git":
        payload["missing"] = [
            relative for relative in REQUIRED_FILES if not (Path(info["path"]) / relative).is_file()
        ]
    return payload
