"""Re-exec scaffold entry points under the workspace ``.venv`` interpreter.

System ``python3`` cannot see packages installed by ``uv sync``. Entry scripts
already put ``.agents/lib`` on ``sys.path``; call
:func:`ensure_workspace_interpreter` immediately after that insert.

If a sentinel package cannot be imported and ``.venv/bin/python`` exists, this
module ``execve``s the same script under that interpreter. ``VAWS_VENV_REEXEC``
prevents recursion. ``VAWS_SKIP_VENV_REEXEC=1`` disables the hop.

This module never runs ``uv sync``. A missing ``.venv`` is an error whose
remedy is ``uv sync``. ``uv run python3 .agents/scripts/<entry>.py`` is the
equivalent form that creates the environment first.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REEXEC_ENV = "VAWS_VENV_REEXEC"
SKIP_ENV = "VAWS_SKIP_VENV_REEXEC"
SENTINEL_PACKAGES = ("remote_dev", "vaws_coordinator", "vaws_knowledge")
REMEDY = "uv sync"


def workspace_venv_python(repo_root: Path) -> Path:
    return Path(repo_root) / ".venv" / "bin" / "python"


def _packages_importable() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in SENTINEL_PACKAGES)


def ensure_workspace_interpreter(*, repo_root: Path) -> None:
    """Switch to ``.venv/bin/python`` when workspace packages are not importable."""
    if os.environ.get(SKIP_ENV) == "1":
        return
    if os.environ.get(REEXEC_ENV) == "1":
        return
    if _packages_importable():
        return
    venv_python = workspace_venv_python(repo_root)
    if venv_python.is_file():
        env = os.environ.copy()
        env[REEXEC_ENV] = "1"
        executable = os.fsdecode(venv_python)
        os.execve(executable, [executable, *sys.argv], env)
    sys.stderr.write(
        "workspace packages are not importable and .venv/bin/python is missing; "
        f"install them with `{REMEDY}` "
        "(or `uv run python3 .agents/scripts/<entry>.py`, which syncs first).\n"
    )
    raise SystemExit(2)
