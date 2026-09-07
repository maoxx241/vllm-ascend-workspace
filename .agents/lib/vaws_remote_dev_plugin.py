"""remote-dev endpoint resolver for VAWS machines, sessions and worktree bindings.

The remote-dev substrate resolves explicit ``host`` + ``port`` (and ``alias``)
itself and hands every other payload to registered resolvers. This plugin is
the scaffold's resolver. It reproduces, on the side that actually owns machine
inventories and session bindings, what the substrate's former
``core/endpoint.py::_endpoint_from_managed`` did:

* ``machine``       -> the container SSH endpoint of an inventory record
* ``session_id`` /
  ``session_file``  -> the container SSH endpoint of a managed session
* no selector       -> the session bound to the nearest worktree
                       (``.vaws-local/current-session.json``, walking upward
                       from the process cwd and stopping at the repo root);
                       declined with ``None`` when no binding exists so the
                       substrate produces its own "no endpoint target" error

Every endpoint it returns carries ``runtime_env_file`` set to the Ascend
profile the workspace containers install, so remote commands keep running
with the Ascend environment the old in-tree copy sourced unconditionally.

Load it into a substrate process with::

    REMOTE_DEV_RESOLVERS=/abs/path/.agents/lib/vaws_remote_dev_plugin.py:setup

(`.agents/scripts/remote_dev.py` sets this for the MCP server, CLI wrappers
and hooks), or call :func:`setup` after importing ``core.endpoint`` in an
embedding process. Nothing from the substrate is imported at module import
time: the mapping is testable without a checkout, and the substrate's
``core`` package is only required when a resolution actually happens inside
a substrate process.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_remote_dev import ASCEND_RUNTIME_ENV_FILE  # noqa: E402
from vaws_remote_toolbox import RemoteToolboxError, resolve_remote_target  # noqa: E402

REPO_ROOT = LIB.parents[1]
RESOLVER_NAME = "vaws"
FIELDS: tuple[str, ...] = ("machine", "session_id", "session_file")
DESCRIPTION = (
    "VAWS managed targets: `machine` from the shared inventory, `session_id` / "
    "`session_file` from managed sessions, or the current worktree's session "
    "binding when no selector is given."
)


def _endpoint_error(message: str, cause: BaseException | None = None) -> Exception:
    """Build the substrate's ``EndpointError`` when it is importable.

    Outside a substrate process (unit tests of the mapping) fall back to a
    plain ``RuntimeError``; the substrate wraps foreign exceptions into an
    ``EndpointError`` anyway.
    """
    try:
        from core.errors import EndpointError  # type: ignore[import-not-found]
    except ImportError:
        EndpointError = RuntimeError  # type: ignore[assignment]  # noqa: N806
    error = EndpointError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


def endpoint_payload(target: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Map a ``RemoteTarget`` to the endpoint dict the substrate expects.

    Only fields the scaffold owns are set here. Caller overrides (``root``,
    ``cwd``, ``user``, ``runtime_env``, ``identity_file``, ...) are merged by
    the substrate on top of this dict, exactly as the old code let the payload
    win over the managed target.
    """
    endpoint = target.container_endpoint
    session_id = getattr(target, "session_id", None)
    return {
        "host": endpoint.host,
        "port": int(endpoint.port),
        "user": endpoint.user,
        "cwd": target.runtime_root,
        "runtime_env_file": ASCEND_RUNTIME_ENV_FILE,
        "kind": "managed-session" if session_id else "managed-machine",
        "alias": str(payload.get("session_id") or payload.get("machine") or session_id or target.alias),
        "source": {"vaws_target": target.to_dict()},
    }


def resolve_vaws(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Resolver entry point: ``dict`` when claimed, ``None`` when not ours."""
    selectors = {field: payload.get(field) for field in FIELDS if payload.get(field)}
    try:
        target = resolve_remote_target(repo_root=REPO_ROOT, **{field: payload.get(field) for field in FIELDS})
    except RemoteToolboxError as exc:
        if not selectors:
            # No selector and no worktree binding: this payload is not ours.
            return None
        raise _endpoint_error(f"failed to resolve managed target {selectors}: {exc}", exc) from exc
    except Exception as exc:  # noqa: BLE001 - corrupt session/inventory files
        if not selectors:
            return None
        raise _endpoint_error(f"failed to resolve managed target {selectors}: {type(exc).__name__}: {exc}", exc) from exc
    return endpoint_payload(target, payload)


def setup() -> None:
    """Register the resolver with the substrate (a ``REMOTE_DEV_RESOLVERS`` setup hook)."""
    from core.endpoint import register_resolver  # type: ignore[import-not-found]

    register_resolver(resolve_vaws, name=RESOLVER_NAME, fields=FIELDS, description=DESCRIPTION)


# Equivalent to the substrate's `@resolver_setup` decorator without importing
# `core.endpoint` at module import time (see the module docstring).
setup.remote_dev_resolver_setup = True  # type: ignore[attr-defined]
