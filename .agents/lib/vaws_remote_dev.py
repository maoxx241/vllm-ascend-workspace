"""Wire the installed ``vaws-remote-dev`` package with scaffold environment.

The package is imported from site-packages. This module no longer locates a
git checkout and does not read a former checkout-root environment variable.
It still builds the environment the MCP server needs, and it is the only
scaffold place that may call ``remote_dev.core.ssh_transport``. Skills do
not construct SSH options; they call the helpers here.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, TextIO

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
REQUIRED_TRANSPORT_VERSION = "0.2.0"

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


def apply_consumer_environment(repo_root: Path = ROOT) -> dict[str, str]:
    """Install scaffold remote-dev env into ``os.environ`` for in-process calls."""
    env = substrate_environment(repo_root=repo_root)
    for key, value in env.items():
        if key.startswith("REMOTE_DEV_"):
            os.environ[key] = value
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


def require_transport(repo_root: Path = ROOT):
    """Import v0.2.0 stream APIs or fail with the cause and remedy.

    Does not catch ``ImportError`` and continue. A missing package or a
    pre-v0.2.0 install cannot silently fall back to raw ``ssh``.
    """
    require_package(repo_root=repo_root)
    apply_consumer_environment(repo_root=repo_root)
    try:
        from remote_dev.core.endpoint import Endpoint
        from remote_dev.core.errors import RemoteExecutionError
        from remote_dev.core.ssh_transport import (
            run_bytes,
            run_script,
            run_stream,
            ssh_base_cmd,
            stream_ssh_command,
        )
    except ImportError as exc:
        raise RemoteDevUnavailable(
            f"{PACKAGE} is installed but missing required transport APIs ({exc}). "
            f"Pin {PACKAGE}=={REQUIRED_TRANSPORT_VERSION} and run `{REMEDY}`."
        ) from exc
    if not hasattr(Endpoint, "for_long_stream"):
        raise RemoteDevUnavailable(
            f"{PACKAGE} is installed but Endpoint.for_long_stream is missing; "
            f"need {REQUIRED_TRANSPORT_VERSION}+. Run `{REMEDY}` after pinning "
            f"{PACKAGE}=={REQUIRED_TRANSPORT_VERSION}."
        )
    return {
        "Endpoint": Endpoint,
        "RemoteExecutionError": RemoteExecutionError,
        "run_bytes": run_bytes,
        "run_script": run_script,
        "run_stream": run_stream,
        "ssh_base_cmd": ssh_base_cmd,
        "stream_ssh_command": stream_ssh_command,
    }


def as_endpoint(
    host: str,
    port: int,
    user: str = "root",
    *,
    long_stream: bool = False,
    connect_timeout_s: int | None = None,
    cwd: str | None = None,
    identity_file: str | None = None,
    ssh_mux: bool | None = None,
):
    """Build a remote-dev ``Endpoint``. Option construction stays in the package."""
    api = require_transport()
    Endpoint = api["Endpoint"]
    kwargs: dict[str, Any] = {
        "host": str(host),
        "port": int(port),
        "user": str(user or "root"),
        "runtime_env_file": ASCEND_RUNTIME_ENV_FILE,
    }
    if connect_timeout_s is not None:
        kwargs["connect_timeout_ms"] = max(1, int(connect_timeout_s)) * 1000
    if cwd:
        kwargs["cwd"] = cwd
    if identity_file:
        kwargs["identity_file"] = identity_file
    if long_stream:
        return Endpoint.for_long_stream(**kwargs)
    if ssh_mux is not None:
        kwargs["ssh_mux"] = ssh_mux
    return Endpoint(**kwargs)


def endpoint_from(
    endpoint: Any,
    *,
    long_stream: bool = False,
    connect_timeout_s: int | None = None,
    cwd: str | None = None,
    identity_file: str | None = None,
    ssh_mux: bool | None = None,
):
    """Accept a remote-dev ``Endpoint`` or a host/port/user duck type."""
    api = require_transport()
    Endpoint = api["Endpoint"]
    if isinstance(endpoint, Endpoint):
        if long_stream:
            return Endpoint.for_long_stream(
                host=endpoint.host,
                port=endpoint.port,
                user=endpoint.user,
                cwd=endpoint.cwd or cwd,
                runtime_env_file=endpoint.runtime_env_file or ASCEND_RUNTIME_ENV_FILE,
                identity_file=endpoint.identity_file or identity_file,
                connect_timeout_ms=endpoint.connect_timeout_ms,
            )
        return endpoint
    return as_endpoint(
        endpoint.host,
        int(endpoint.port),
        getattr(endpoint, "user", "root") or "root",
        long_stream=long_stream,
        connect_timeout_s=connect_timeout_s,
        cwd=cwd,
        identity_file=identity_file,
        ssh_mux=ssh_mux,
    )


def ssh_argv(
    endpoint: Any,
    *,
    long_stream: bool = False,
    connect_timeout_s: int | None = None,
    identity_file: str | None = None,
    ssh_mux: bool | None = None,
) -> list[str]:
    """Composed SSH argv from the package. No option construction here."""
    api = require_transport()
    ep = endpoint_from(
        endpoint,
        long_stream=long_stream,
        connect_timeout_s=connect_timeout_s,
        identity_file=identity_file,
        ssh_mux=ssh_mux,
    )
    return list(api["ssh_base_cmd"](ep))


def stream_argv(
    endpoint: Any,
    script: str,
    *,
    timeout: float | None = None,
    connect_timeout_s: int | None = 15,
) -> list[str]:
    """Argv for an attached stream. Refuses a multiplexed endpoint."""
    api = require_transport()
    ep = endpoint_from(endpoint, long_stream=True, connect_timeout_s=connect_timeout_s)
    timeout_ms = None if timeout is None else int(timeout * 1000)
    return list(api["stream_ssh_command"](ep, script, timeout_ms=timeout_ms))


def ssh_exec(
    endpoint: Any,
    script: str,
    *,
    check: bool = True,
    timeout: float | None = 180,
    connect_timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    """Short remote command via ``run_script``. Default connection is multiplexed."""
    api = require_transport()
    ep = endpoint_from(endpoint, connect_timeout_s=connect_timeout)
    timeout_ms = None if timeout is None else int(timeout * 1000)
    completed = api["run_script"](ep, script, timeout_ms=timeout_ms)
    cmd = [*api["ssh_base_cmd"](ep), "bash", "-s"]
    if completed.timed_out:
        result = subprocess.CompletedProcess(
            cmd, 255, completed.stdout or "", f"ssh_exec timed out after {timeout}s"
        )
    else:
        result = subprocess.CompletedProcess(
            cmd,
            0 if completed.returncode is None else int(completed.returncode),
            completed.stdout or "",
            completed.stderr or "",
        )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"remote command failed (rc={result.returncode}):\n"
            f"stderr: {(result.stderr or '')[:2000]}"
        )
    return result


def ssh_stream(
    endpoint: Any,
    script: str,
    *,
    forward_prefix: str = "[remote] ",
    timeout: float | None = None,
    connect_timeout: int = 15,
    output: TextIO | None = None,
) -> int:
    """Hour-scale attached stream via ``Endpoint.for_long_stream`` + ``run_stream``."""
    api = require_transport()
    ep = endpoint_from(endpoint, long_stream=True, connect_timeout_s=connect_timeout)
    timeout_ms = None if timeout is None else int(timeout * 1000)
    completed = api["run_stream"](
        ep,
        script,
        timeout_ms=timeout_ms,
        forward_prefix=forward_prefix,
        output=output,
    )
    if completed.timed_out:
        raise TimeoutError(
            f"remote command exceeded {timeout}s (no output for the wall-clock window)"
        )
    return 0 if completed.returncode is None else int(completed.returncode)


def ssh_run_bytes(
    endpoint: Any,
    remote_command: str,
    *,
    stdin: bytes | None = None,
    timeout: float | None = None,
    connect_timeout: int = 15,
) -> subprocess.CompletedProcess[bytes]:
    api = require_transport()
    ep = endpoint_from(endpoint, connect_timeout_s=connect_timeout)
    timeout_ms = None if timeout is None else int(timeout * 1000)
    return api["run_bytes"](ep, remote_command, stdin=stdin, timeout_ms=timeout_ms)


def run_cli_tool(tool: str, argv: list[str] | None = None) -> int:
    """Invoke a remote-dev CLI tool after injecting scaffold environment."""
    require_transport()
    from remote_dev.cli import run_tool_main

    return run_tool_main(tool, argv)
