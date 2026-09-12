"""Enter the prepared native coordinator owner before a business CLI starts.

This only replaces a WSL CLI process with its prepared Windows interpreter.
It does not proxy TaskClient, create a task, start a daemon, or replay a running
business function. Native local analysis and explicit remote-dev I/O stay native.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from collections.abc import Sequence

from vaws_local_owner import accessible_windows_path, managed_path, windows_interop_env, windows_mounted_workspace

PATH_ENV = frozenset({
    "VAWS_AGENT_SESSIONS_DIR", "VAWS_COORDINATOR_STATE_DIR", "VAWS_CONTEXT_FILE",
    "VAWS_GITHUB_IDENTITY_FILE",
    "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT", "REMOTE_DEV_STATE_DIR",
})
IDENTITY_ENV = frozenset({"CODEX_THREAD_ID", "CODEX_SESSION_ID"})
OPAQUE_SECTIONS = frozenset({"--", "--serve-args", "--bench-args", "--extra-serve-args"})


class ManagedEntryError(RuntimeError):
    """The managed owner cannot be entered before business work begins."""


def _windows_executable(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z]:[\\/].*", value):
        raise ManagedEntryError("the Windows ready receipt has no mounted-drive interpreter")
    windows = PureWindowsPath(value)
    return str(PurePosixPath("/mnt") / windows.drive[0].lower() / PurePosixPath(*windows.parts[1:]))


def _local_argument(value: str) -> str:
    # Relative CLI paths stay relative to the same actual mounted-drive cwd.
    # Remote shell text never reaches this function: callers list local options.
    if value.startswith("/"):
        try:
            return managed_path(value, windows=True)
        except ValueError as exc:
            raise ManagedEntryError("managed local paths must be on a mounted Windows drive") from exc
    return value


def local_arguments(arguments: Sequence[str], local_options: Sequence[str]) -> list[str]:
    """Translate only declared local path options; passthrough sections are opaque."""
    names = {"--context-file", *local_options}
    result = []
    local = False
    opaque = False
    for value in arguments:
        if opaque:
            result.append(value)
        elif value in OPAQUE_SECTIONS:
            result.append(value)
            opaque = True
        elif local:
            result.append(_local_argument(value))
            local = False
        else:
            name, equal, argument = value.partition("=")
            if name in names:
                result.append(name + "=" + _local_argument(argument) if equal else value)
                local = not equal
            else:
                result.append(value)
    return result


def managed_invocation(entry_file: str, receipt: dict, *, local_options: Sequence[str] = (),
                       original: Sequence[str] | None = None, environment=None,
                       owner_python: str | None = None) -> tuple[list[str], dict[str, str]]:
    """Build one native process replacement, preserving Python flags and payloads."""
    from vaws_environment import MANAGED_PIN_ENV, PIN_ENV

    values = list(original if original is not None else getattr(sys, "orig_argv", [sys.executable, *sys.argv]))[1:]
    prefix = []
    index = 0
    while index < len(values):
        value = values[index]
        if value == "-m":
            if index + 1 == len(values):
                raise ManagedEntryError("Python -m is missing its module")
            prefix.extend(values[index:index + 2])
            index += 2
            break
        if value in {"-c", "-"}:
            raise ManagedEntryError("enter the managed owner from a script or module CLI before reading stdin")
        if value == "--":
            prefix.append(value)
            index += 1
            if index == len(values):
                raise ManagedEntryError("Python has no script entry")
            prefix.append(managed_path(Path(entry_file).resolve(), windows=True))
            index += 1
            break
        if value.startswith("-"):
            prefix.append(value)
            index += 1
            if value in {"-X", "-W", "--check-hash-based-pycs"} and index < len(values):
                prefix.append(values[index])
                index += 1
            continue
        prefix.append(managed_path(Path(entry_file).resolve(), windows=True))
        index += 1
        break
    else:
        raise ManagedEntryError("Python has no script or module entry")
    arguments = local_arguments(values[index:], local_options)
    env = dict(os.environ if environment is None else environment)
    # WSL can restore variables from its Windows parent when they are absent
    # here. Explicit empty /w entries preserve the caller's missing identity.
    forwarded = {key: managed_path(env[key], windows=True) if env.get(key) else ""
                 for key in PATH_ENV}
    forwarded.update({key: env.get(key, "") for key in IDENTITY_ENV})
    forwarded.update({key: value for key, value in env.items()
                      if key.startswith("REMOTE_DEV_") and key not in PATH_ENV})
    forwarded[PIN_ENV] = receipt["receipt"]
    forwarded[MANAGED_PIN_ENV] = receipt["receipt"]
    if env.get("WSLENV"):
        fixed = PATH_ENV | IDENTITY_ENV | {PIN_ENV, MANAGED_PIN_ENV}
        forwarded["WSLENV"] = ":".join(part for part in env["WSLENV"].split(":")
                                       if part and part.split("/", 1)[0] not in fixed)
    env.update(windows_interop_env(forwarded))
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "VAWS_VENV_REEXEC"):
        env.pop(key, None)
    return [owner_python or _windows_executable(receipt["python"]), "-X", "utf8", *prefix, *arguments], env


def ensure_managed_entry(*, repo_root: Path, entry_file: str,
                         local_options: Sequence[str] = ()) -> None:
    """A CLI calls this before reading input or doing business I/O.

    An imported module cannot restart its caller. Such consumers must enter the
    owner at their own CLI boundary, or use the native coordinator SDK there.
    """
    if not windows_mounted_workspace(repo_root):
        return
    for argument in sys.argv[1:]:
        if argument in OPAQUE_SECTIONS:
            break
        if argument in {"-h", "--help"}:
            return
    main = getattr(sys.modules.get("__main__"), "__file__", None)
    if main is None or Path(main).resolve() != Path(entry_file).resolve():
        raise ManagedEntryError("an imported business module cannot restart its caller; enter the managed owner at the CLI boundary")
    from vaws_environment import EnvironmentError, windows_ready

    try:
        receipt = windows_ready(repo_root, use_saved=True)
        command, environment = managed_invocation(entry_file, receipt, local_options=local_options,
                                                  owner_python=accessible_windows_path(receipt["python"]))
        if not Path(command[0]).is_file():
            raise ManagedEntryError("the prepared Windows owner interpreter is missing")
    except (EnvironmentError, ManagedEntryError, ValueError) as exc:
        print(f"VAWS managed entry unavailable: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    # exec preserves stdin, stdout, stderr and the native exit status. No
    # business work has occurred in this process and there is no second runner.
    os.execve(command[0], command, environment)


def require_managed_owner(repo_root: Path) -> None:
    """Guard SDK imports without guessing an identity or replaying business work."""
    if windows_mounted_workspace(repo_root):
        raise ManagedEntryError("this TaskClient call requires the managed owner; its CLI must enter that owner before business I/O")
