"""Locate and wire the remote-dev substrate, which lives in its own repository.

`vllm-ascend-workspace/remote-dev` used to be vendored at ``.remote-dev/`` and
reached backwards into ``.agents/lib`` to resolve ``machine`` / ``session_id``
selectors. The extraction inverted that dependency: the substrate now resolves
explicit ``host`` + ``port`` only and asks *registered resolvers* for anything
else. This module is the scaffold's side of that contract:

* it finds the substrate checkout (``VAWS_REMOTE_DEV_ROOT``, else the shared
  workspace's ``.vaws-local/remote-dev``), never a path inside this tree;
* it builds the environment the substrate needs to behave the way the old
  in-tree copy did: the scaffold resolver plugin, the Ascend runtime
  environment file that every remote command must source, a state directory
  under the scaffold's own untracked state, and the SSH multiplexing directory
  shared with ``vaws_ssh``;
* it exposes the dependency pin (``.agents/deps/remote-dev.json``) so callers
  can report drift instead of silently running an unknown revision.

The substrate is consumed as an external checkout rather than a submodule or a
vendored copy. It is a private repository; a private ``.gitmodules`` entry
would break ``git submodule update --init --recursive`` for every public
cloner of this scaffold. The tracked pin in ``.agents/deps/remote-dev.json``
names the intended revision; bump ``commit`` deliberately, and
``remote_dev.py status`` reports drift. Import direction is scaffold ->
substrate only; nothing here is imported by the substrate.
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
    USABLE_STATES,
    checkout_path,
    inspect,
    load_pin,
    load_pin_file,
)
REMOTE_DEV_ROOT_ENV = "VAWS_REMOTE_DEV_ROOT"
DEPENDENCY_FILE = ROOT / ".agents" / "deps" / "remote-dev.json"
RESOLVER_PLUGIN = LIB / "vaws_remote_dev_plugin.py"
RESOLVER_SETUP = "setup"
CHECKOUT_DIRNAME = "remote-dev"
STATE_DIRNAME = "remote-dev-state"
LOCAL_STATE_DIRNAME = ".vaws-local"

# The Ascend toolchain profile that the workspace-managed containers install.
# The old in-tree substrate hard-coded it; the standalone one sources only what
# `Endpoint.runtime_env_file` names, so the scaffold has to say it explicitly.
ASCEND_RUNTIME_ENV_FILE = "/etc/profile.d/vaws-ascend-env.sh"

# Same ControlMaster directory as `vaws_ssh.py`, so the substrate and the
# managed toolbox share one multiplexed connection per endpoint.
SSH_MUX_DIR = "~/.ssh/vaws-mux"

REQUIRED_FILES = ("mcp/server.py", "core/endpoint.py", "core/shell_ops.py", "tools/_cli.py")

# Environment keys this module owns defaults for. Anything the caller (client
# config, shell) already set wins; see `substrate_environment`. The
# `REMOTE_DEV_DEFAULT_USER/ROOT/CWD` permission defaults are deliberately not
# here: the client configuration files set them for MCP sessions, and the CLI
# wrappers keep the substrate's own defaults, exactly as before the extraction.
DEFAULT_ENV = {
    "REMOTE_DEV_RUNTIME_ENV_FILE": ASCEND_RUNTIME_ENV_FILE,
    "REMOTE_DEV_SSH_MUX_DIR": SSH_MUX_DIR,
}


class RemoteDevUnavailable(RuntimeError):
    """No usable remote-dev checkout is configured."""


def load_dependency(path: Path = DEPENDENCY_FILE) -> dict[str, Any]:
    """Return the tracked dependency pin (repository, ref, commit)."""
    if path == DEPENDENCY_FILE:
        return load_pin("remote-dev")
    return load_pin_file(path)


def default_checkout_dir(repo_root: Path = ROOT) -> Path:
    """Where `remote_dev.py bootstrap` clones the substrate by default.

    One checkout per shared workspace, next to the machine inventory, so every
    linked session worktree reaches the same revision.
    """
    path, _source = checkout_path("remote-dev", env={}, repo_root=repo_root)
    return path


def looks_like_checkout(path: Path) -> bool:
    return all((path / relative).is_file() for relative in REQUIRED_FILES)


def _configured_root(env: Mapping[str, str]) -> tuple[Path | None, str]:
    path, source = checkout_path("remote-dev", env=env)
    return path, source


def remote_dev_root(*, required: bool = True, env: Mapping[str, str] | None = None) -> Path | None:
    """Resolve the substrate checkout root.

    Order: ``VAWS_REMOTE_DEV_ROOT``, then the default checkout directory. A
    configured path that does not look like a checkout is an error either way;
    a missing default is an error only when ``required``. Identity drift
    (``off_pin``, ``wrong_origin``) is not an execution gate.
    """
    env = os.environ if env is None else env
    info = inspect("remote-dev", env)
    if info["state"] in USABLE_STATES:
        return Path(info["path"]).expanduser().resolve()
    if not required:
        return None
    pin = load_pin("remote-dev")
    hint = f"clone it with `{pin['bootstrap']}` or set {pin['root_env']}"
    candidate = Path(info["path"])
    if info["source"] == "env":
        raise RemoteDevUnavailable(
            f"{REMOTE_DEV_ROOT_ENV}={candidate} is not a remote-dev checkout "
            f"(state={info['state']}); {hint}"
        )
    raise RemoteDevUnavailable(f"remote-dev checkout not found at {candidate}; {hint}")


def add_substrate_to_path(root: Path | None = None, *, prepend: bool = False) -> Path:
    """Make ``core`` / ``mcp`` / ``tools`` importable from the checkout.

    Appends by default: the substrate ships an unrelated ``mcp/`` package that
    must not shadow the installed MCP Python SDK in processes that import both
    (the coordinator server does).
    """
    resolved = (root or remote_dev_root()).resolve()
    entry = str(resolved)
    if entry not in sys.path:
        if prepend:
            sys.path.insert(0, entry)
        else:
            sys.path.append(entry)
    return resolved


def state_dir(repo_root: Path = ROOT) -> Path:
    """Local remote-dev state (job records, read ledgers, logs) for this checkout.

    Per checkout, like the former ``.remote-dev/state``, but under the
    scaffold's own ignored state directory.
    """
    return repo_root / LOCAL_STATE_DIRNAME / STATE_DIRNAME


def resolver_spec(repo_root: Path = ROOT) -> str:
    return f"{repo_root / '.agents' / 'lib' / RESOLVER_PLUGIN.name}:{RESOLVER_SETUP}"


def _absolute_resolver_specs(raw: str, repo_root: Path) -> str:
    """Resolve relative ``/path/plugin.py:callable`` entries against the repo root.

    Client configuration files are tracked and therefore carry repo-relative
    paths; the substrate resolves paths against its process cwd, which is not
    always the project root (Codex and Grok set their own).
    """
    entries: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        module_spec, sep, attr = item.rpartition(":")
        if sep and module_spec.endswith(".py") and not Path(module_spec).expanduser().is_absolute():
            module_spec = str((repo_root / module_spec).resolve())
            item = f"{module_spec}:{attr}"
        entries.append(item)
    return ",".join(entries)


def substrate_environment(base: Mapping[str, str] | None = None, *, repo_root: Path = ROOT) -> dict[str, str]:
    """Environment for a substrate process (MCP server, CLI wrapper, hook).

    Starts from ``base`` (default: the current process environment), fills the
    keys the substrate needs, and turns repo-relative resolver / state paths
    into absolute ones. Values already present are kept, so a client config
    or a shell can override any default.
    """
    env = dict(os.environ if base is None else base)
    for key, value in DEFAULT_ENV.items():
        env.setdefault(key, value)
    env.setdefault("REMOTE_DEV_RESOLVERS", resolver_spec(repo_root))
    env["REMOTE_DEV_RESOLVERS"] = _absolute_resolver_specs(env["REMOTE_DEV_RESOLVERS"], repo_root)
    env.setdefault("REMOTE_DEV_STATE_DIR", str(state_dir(repo_root)))
    state = Path(env["REMOTE_DEV_STATE_DIR"]).expanduser()
    if not state.is_absolute():
        state = repo_root / state
    env["REMOTE_DEV_STATE_DIR"] = str(state)
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
    info = inspect("remote-dev", env, repo_root=repo_root)
    pin = load_dependency()
    payload: dict[str, Any] = {
        "root": info["path"],
        "root_source": info["source"],
        "root_env": REMOTE_DEV_ROOT_ENV,
        "repository": pin.get("repository"),
        "pinned_ref": pin.get("ref"),
        "pinned_commit": pin.get("commit"),
        "resolver": resolver_spec(repo_root),
        "runtime_env_file": env.get("REMOTE_DEV_RUNTIME_ENV_FILE", ASCEND_RUNTIME_ENV_FILE),
        "state_dir": str(state_dir(repo_root)),
        "state": info["state"],
        "commit": info["commit"],
        "pin_matches": info["pin_matches"],
        "origin_matches": info["origin_matches"],
        "problems": info["problems"],
    }
    if info["state"] in {"not_git", "incomplete"}:
        payload["missing"] = [
            relative for relative in REQUIRED_FILES if not (Path(info["path"]) / relative).is_file()
        ]
    return payload
