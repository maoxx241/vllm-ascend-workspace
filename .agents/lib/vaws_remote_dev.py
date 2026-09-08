"""Wire the installed ``vaws-remote-dev`` package with scaffold environment.

The package is imported from site-packages. This module no longer locates a
git checkout and does not read a former checkout-root environment variable.
It still builds the
environment the MCP server needs: the scaffold resolver plugin, the Ascend
runtime profile, the untracked state directory, and the shared SSH mux dir.
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

from vaws_dependency import REMEDY, USABLE_STATES, inspect  # noqa: E402

PACKAGE = "vaws-remote-dev"
RESOLVER_PLUGIN = LIB / "vaws_remote_dev_plugin.py"
RESOLVER_SETUP = "setup"
STATE_DIRNAME = "remote-dev-state"
LOCAL_STATE_DIRNAME = ".vaws-local"
ASCEND_RUNTIME_ENV_FILE = "/etc/profile.d/vaws-ascend-env.sh"
SSH_MUX_DIR = "~/.ssh/vaws-mux"

DEFAULT_ENV = {
    "REMOTE_DEV_RUNTIME_ENV_FILE": ASCEND_RUNTIME_ENV_FILE,
    "REMOTE_DEV_SSH_MUX_DIR": SSH_MUX_DIR,
}


class RemoteDevUnavailable(RuntimeError):
    """The vaws-remote-dev package is not usable in this interpreter."""


def state_dir(repo_root: Path = ROOT) -> Path:
    """Local remote-dev state (job records, read ledgers, logs)."""
    return repo_root / LOCAL_STATE_DIRNAME / STATE_DIRNAME


def resolver_spec(repo_root: Path = ROOT) -> str:
    return f"{repo_root / '.agents' / 'lib' / RESOLVER_PLUGIN.name}:{RESOLVER_SETUP}"


def _absolute_resolver_specs(raw: str, repo_root: Path) -> str:
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
    """Environment for a substrate process (MCP server, CLI wrapper, hook)."""
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


def package_status(env: Mapping[str, str] | None = None, *, repo_root: Path = ROOT) -> dict[str, Any]:
    """Describe the installed remote-dev package."""
    del env
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
        "resolver": resolver_spec(repo_root),
        "runtime_env_file": ASCEND_RUNTIME_ENV_FILE,
        "state_dir": str(state_dir(repo_root)),
    }


def require_package(repo_root: Path = ROOT) -> dict[str, Any]:
    info = inspect(PACKAGE, repo_root=repo_root)
    if info["state"] not in USABLE_STATES:
        raise RemoteDevUnavailable(
            f"{PACKAGE} is {info['state']}; install it with `{REMEDY}`"
        )
    return info
