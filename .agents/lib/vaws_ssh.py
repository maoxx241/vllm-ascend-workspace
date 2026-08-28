"""Shared SSH client options for all VAWS scaffold tooling.

Every SSH invocation in the scaffold goes through many short-lived commands
against the same few endpoints, so connection reuse via OpenSSH ControlMaster
is the single biggest latency win. This module is the one place that defines
the common option set; all skill/lib SSH builders should use it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_MUX_DIR = Path.home() / ".ssh" / "vaws-mux"

# Cache the mux-dir readiness decision once per process: the directory setup is
# a filesystem syscall that would otherwise run on every SSH command build, and
# the outcome cannot change mid-process in a way we want to keep retrying.
# ``None`` = not yet decided, ``True``/``False`` = usable / not usable.
_MUX_READY: bool | None = None


def _ensure_mux_dir() -> bool:
    """Prepare the ControlMaster mux dir once; warn (not silently) on failure.

    Connection reuse is the single biggest latency win for this tooling, so a
    disabled mux is a real (if non-fatal) degradation. Plain SSH still works,
    so we do not hard-fail, but we surface exactly one stderr warning instead
    of silently dropping to slow per-command connections.
    """
    global _MUX_READY
    if _MUX_READY is not None:
        return _MUX_READY
    try:
        _MUX_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(_MUX_DIR, 0o700)
        _MUX_READY = True
    except OSError as exc:
        sys.stderr.write(
            f"[vaws_ssh] WARNING: SSH ControlMaster disabled; could not prepare "
            f"{_MUX_DIR} ({exc}). Every SSH command will pay a fresh connection "
            f"handshake (much slower). Fix the ~/.ssh permissions to restore reuse.\n"
        )
        _MUX_READY = False
    return _MUX_READY


def control_master_options() -> list[str]:
    """OpenSSH connection-reuse options with a per-connection socket path.

    ``%C`` hashes local host, remote host, port, and user, keeping the socket
    path short (unix socket paths are limited to ~104 chars on macOS).
    """
    if not _ensure_mux_dir():
        return []
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={_MUX_DIR}/%C",
        "-o", "ControlPersist=120",
    ]


def base_ssh_options(*, connect_timeout: int | None = None) -> list[str]:
    """Non-interactive, fail-fast SSH options shared by all scaffold tools."""
    options = [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
    ]
    if connect_timeout is not None:
        options.extend(["-o", f"ConnectTimeout={max(1, connect_timeout)}"])
    options.extend(control_master_options())
    return options
