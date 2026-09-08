"""Locate and import the coordinator-owned host NPU queue module.

The scaffold does not keep a second copy. Every in-process reader resolves
``host/vaws_npu_coordination.py`` from the pinned vaws-coordinator checkout
through the same locator as every other consumer.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Mapping

from vaws_dependency import DependencyUnavailable, load_pin, resolve

HOST_QUEUE_RELATIVE = "host/vaws_npu_coordination.py"
CAPABILITY = "host_npu_authority"


class HostQueueUnavailable(RuntimeError):
    """The coordinator-owned host queue module cannot be resolved."""


def host_queue_module_path(
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> Path:
    """Return ``<coordinator checkout>/host/vaws_npu_coordination.py``."""
    kwargs: dict[str, Any] = {"required": True, "env": env}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    pin = load_pin("vaws-coordinator")
    try:
        root = resolve("vaws-coordinator", **kwargs)
    except DependencyUnavailable as exc:
        raise HostQueueUnavailable(
            f"capability {CAPABILITY} is unavailable: {exc}"
        ) from exc
    assert root is not None
    path = root / HOST_QUEUE_RELATIVE
    if not path.is_file():
        raise HostQueueUnavailable(
            f"capability {CAPABILITY} is unavailable: {HOST_QUEUE_RELATIVE} "
            f"is missing from {root}; clone it with `{pin['bootstrap']}` "
            f"or set {pin['root_env']}"
        )
    return path


def load_host_protocol(
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> Any:
    """Import the coordinator host-queue module by path. No cache."""
    path = host_queue_module_path(env, repo_root=repo_root)
    spec = importlib.util.spec_from_file_location("vaws_host_queue_protocol", path)
    if spec is None or spec.loader is None:
        raise HostQueueUnavailable(
            f"capability {CAPABILITY} is unavailable: cannot import {path}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
