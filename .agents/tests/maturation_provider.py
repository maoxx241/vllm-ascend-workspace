"""Resolve the pinned remote-dev checkout through the scaffold locator.

Joint tests must not hard-code an acceptance-machine path. Call
:func:`require_pinned_provider`: a missing unconfigured default skips like
``SubstrateIntegrationTests``; an explicit missing, malformed, dirty, or
wrong-revision checkout fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_remote_dev as remote_dev  # noqa: E402


def _porcelain(root: Path) -> str:
    return subprocess.check_output(
        ["git", "status", "--porcelain=v1"],
        cwd=str(root),
        text=True,
    ).strip()


def require_pinned_provider() -> Path:
    """Return the locator checkout if it matches the tracked pin and is clean.

    Unconfigured missing default: ``unittest.SkipTest``. Explicit configured
    checkout that is missing, malformed, dirty, or at the wrong revision:
    ``AssertionError`` (never a skip).
    """
    pin = remote_dev.load_dependency()
    pinned = pin.get("commit")
    configured = os.environ.get(remote_dev.REMOTE_DEV_ROOT_ENV, "").strip()
    try:
        if configured:
            root = remote_dev.remote_dev_root(required=True)
        else:
            root = remote_dev.remote_dev_root(required=False)
    except remote_dev.RemoteDevUnavailable as exc:
        raise AssertionError(str(exc)) from exc
    if root is None:
        raise unittest.SkipTest("no remote-dev checkout (set VAWS_REMOTE_DEV_ROOT)")
    commit = remote_dev.checkout_commit(root)
    if commit != pinned:
        raise AssertionError(
            f"remote-dev checkout {root} HEAD {commit} does not match tracked pin {pinned}"
        )
    dirty = _porcelain(root)
    if dirty:
        raise AssertionError(f"remote-dev checkout {root} is not clean")
    return root
