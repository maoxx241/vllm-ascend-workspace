"""Select the immutable environment pinned by this client or current inputs.

The bootstrap command is ``uv run --no-project python .agents/scripts/vaws_deps.py sync``. Windows
and WSL environments coexist in the per-user content-addressed store. Interpreter
flags and module entry points survive the hop. Native Windows owns the child
process tree and emits UTF-8 JSON independently of the terminal code page.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from vaws_environment import EnvironmentError, PIN_ENV, native_ready

REEXEC_ENV = "VAWS_VENV_REEXEC"
SKIP_ENV = "VAWS_SKIP_VENV_REEXEC"
SENTINEL_PACKAGES = ("remote_dev", "vaws_coordinator")
REMEDY = "uv run --no-project python .agents/scripts/vaws_deps.py sync"


def configure_windows_stdio() -> None:
    """Keep native CLI output stable across Windows display languages."""
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def ensure_workspace_interpreter(
    *, repo_root: Path, packages: tuple[str, ...] = SENTINEL_PACKAGES,
) -> None:
    """Choose by dependency identity; importability alone never selects a runtime."""
    configure_windows_stdio()
    if os.environ.get(SKIP_ENV) == "1":
        return
    try:
        receipt = native_ready(repo_root)
    except EnvironmentError as exc:
        sys.stderr.write(f"{exc}; run `{REMEDY}` before starting a new client.\n")
        raise SystemExit(2) from exc
    venv_python = Path(receipt["python"])
    needs_utf8 = os.name == "nt" and not sys.flags.utf8_mode
    # POSIX bin/python is a symlink to the base executable. Comparing resolved
    # executables alone would falsely accept an unrelated base environment.
    same_environment = Path(sys.prefix).resolve() == Path(receipt["root"]).resolve()
    if same_environment and not needs_utf8:
        os.environ[PIN_ENV] = receipt["receipt"]
        os.environ.pop(REEXEC_ENV, None)
        return
    if not same_environment and os.environ.get(REEXEC_ENV) == receipt["key"]:
        sys.stderr.write("the selected interpreter did not enter its ready environment\n")
        raise SystemExit(2)
    if venv_python.is_file():
        env = os.environ.copy()
        env[REEXEC_ENV] = receipt["key"]
        env[PIN_ENV] = receipt["receipt"]
        env.pop("PYTHONHOME", None)
        env.pop("VIRTUAL_ENV", None)
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
        "the selected ready interpreter is missing; "
        f"install them with `{REMEDY}` "
        "before running the entry again.\n"
    )
    raise SystemExit(2)
