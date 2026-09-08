"""Require the installed vaws-remote-dev package for joint maturation tests.

Joint tests must not hard-code a checkout path. Call
:func:`require_pinned_provider`: a missing install skips; an interpreter that
cannot import ``remote_dev`` after an explicit hide fails.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_dependency import inspect  # noqa: E402
from vaws_remote_dev import RemoteDevUnavailable, require_package  # noqa: E402


def _package_root() -> Path:
    require_package()
    import remote_dev

    origin = getattr(remote_dev, "__file__", None)
    if not origin:
        raise RemoteDevUnavailable("remote_dev has no __file__")
    return Path(origin).resolve().parent


def require_pinned_provider() -> Path:
    """Return the installed package root when the package is usable.

    Unconfigured missing install: ``unittest.SkipTest``. An interpreter that
    was asked to hide the package (``VAWS_SKIP_VENV_REEXEC=1`` plus no
    install) still skips. A broken import after the package metadata says
    ready is ``AssertionError``.
    """
    if importlib.util.find_spec("remote_dev") is None:
        raise unittest.SkipTest("vaws-remote-dev is not installed; run `uv sync`")
    info = inspect("vaws-remote-dev")
    if info["state"] == "missing":
        raise unittest.SkipTest("vaws-remote-dev is not installed; run `uv sync`")
    try:
        return _package_root()
    except RemoteDevUnavailable as exc:
        raise AssertionError(str(exc)) from exc


def require_optional_provider() -> Path:
    """Integration gate: skip when the package is missing, fail if import breaks."""
    if os.environ.get("VAWS_SKIP_VENV_REEXEC") == "1" and importlib.util.find_spec("remote_dev") is None:
        raise unittest.SkipTest("vaws-remote-dev is not installed; run `uv sync`")
    return require_pinned_provider()
