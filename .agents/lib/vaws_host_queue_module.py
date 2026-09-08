"""Thin shim over ``vaws_coordinator.host_queue``.

``session_gc.py`` / ``npu_coordination.py`` ship this module's resolved path
to a remote host as source text. ``host_queue_module_path()`` must therefore
keep returning a real ``Path`` (``Path(module.__file__)`` of the bundled
package file, or an explicit ``VAWS_HOST_QUEUE_MODULE`` override).

Imports from the package are deferred so ``vaws.py status`` can still report
``missing`` when the package is not installed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

HOST_QUEUE_RELATIVE = "vaws_coordinator/host/vaws_npu_coordination.py"
HOST_QUEUE_MODULE_ENV = "VAWS_HOST_QUEUE_MODULE"
CAPABILITY = "host_npu_authority"


class HostQueueUnavailable(RuntimeError):
    """The coordinator host-queue module is not importable."""


def _package():
    try:
        import vaws_coordinator.host_queue as host_queue
    except ImportError as exc:
        raise HostQueueUnavailable(
            "vaws-coordinator.host_queue is not importable; run `uv sync`"
        ) from exc
    return host_queue


def host_queue_module_path(
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
    path: Path | str | None = None,
) -> Path:
    """Return the coordinator-owned host-queue module file."""
    del repo_root
    configured = path
    if configured is None and env is not None:
        configured = env.get(HOST_QUEUE_MODULE_ENV)
    return Path(_package().host_queue_module_path(configured))


def load_host_protocol(
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
    path: Path | str | None = None,
) -> Any:
    del repo_root
    configured = path
    if configured is None and env is not None:
        configured = env.get(HOST_QUEUE_MODULE_ENV)
    return _package().load_host_protocol(configured)


def __getattr__(name: str) -> Any:
    forwarded = {
        "HostQueue",
        "NpuCoordinator",
        "bundled_host_module_path",
        "parse_npu_smi_info",
        "probe_npu_occupancy",
    }
    if name in forwarded:
        return getattr(_package(), name)
    raise AttributeError(name)
