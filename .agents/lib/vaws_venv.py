"""Select the platform-specific workspace environment when packages are missing.

The bootstrap command is ``python .agents/scripts/vaws_deps.py sync``. Windows
and WSL environments coexist below .vaws-local/venvs. Original interpreter
flags and module entry points survive the hop. Native Windows owns the child
process tree and emits UTF-8 JSON independently of the terminal code page.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REEXEC_ENV = "VAWS_VENV_REEXEC"
SKIP_ENV = "VAWS_SKIP_VENV_REEXEC"
SENTINEL_PACKAGES = ("remote_dev", "vaws_coordinator", "vaws_knowledge")
REMEDY = "python .agents/scripts/vaws_deps.py sync"


def workspace_venv_root(repo_root: Path) -> Path:
    """Keep native Windows and WSL interpreters separate in a shared checkout."""
    return Path(repo_root) / ".vaws-local" / "venvs" / sys.platform


def workspace_venv_python(repo_root: Path) -> Path:
    root = workspace_venv_root(repo_root)
    if os.name == "nt":
        windows = root / "Scripts" / "python.exe"
        return windows
    posix = root / "bin" / "python"
    return posix


def _packages_importable() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in SENTINEL_PACKAGES)


def configure_windows_stdio() -> None:
    """Keep native CLI output stable across Windows display languages."""
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def ensure_workspace_interpreter(*, repo_root: Path) -> None:
    """Select this platform's managed environment when it has been installed."""
    configure_windows_stdio()
    if os.environ.get(SKIP_ENV) == "1":
        return
    if os.environ.get(REEXEC_ENV) == "1":
        return
    available = _packages_importable()
    needs_utf8 = os.name == "nt" and not sys.flags.utf8_mode
    if available:
        return
    venv_python = workspace_venv_python(repo_root)
    if Path(sys.executable).absolute() == venv_python.absolute() and not needs_utf8:
        return
    if venv_python.is_file():
        env = os.environ.copy()
        env[REEXEC_ENV] = "1"
        executable = os.fsdecode(venv_python)
        original = getattr(sys, "orig_argv", None)
        if original is None:
            # Bootstrap launchers can predate the workspace's Python minimum.
            main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
            arguments = ["-m", main_spec.name, *sys.argv[1:]] if main_spec else list(sys.argv)
        else:
            arguments = original[1:]
        argv = [executable, *(["-X", "utf8"] if needs_utf8 else []), *arguments]
        if os.name == "nt":
            from vaws_windows import run_owned

            raise SystemExit(run_owned(argv, env=env))
        os.execve(executable, argv, env)
    sys.stderr.write(
        "workspace packages are not importable and the workspace venv python is missing; "
        f"install them with `{REMEDY}` "
        "before running the entry again.\n"
    )
    raise SystemExit(2)
