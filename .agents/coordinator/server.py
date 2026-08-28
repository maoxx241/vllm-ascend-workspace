#!/usr/bin/env python3
"""Independent authenticated Streamable HTTP MCP for prepared VAWS runtimes."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import fcntl
import hashlib
import hmac
import json
import logging
import sys
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_ready_runtime import RuntimePool, safe_id
from vaws_local_state import shared_workspace_root

PRINCIPAL = contextvars.ContextVar("vaws_coordinator_principal")


def create_app(pool, access, *, interval=2.0, allowed_hosts=None):
    principals = access["principals"]
    if not principals:
        raise ValueError("configure at least one bearer-token principal")
    for owner, config in principals.items():
        safe_id(owner)
        if len(config["sha256"]) != 64:
            raise ValueError("access config stores SHA256 digests, never plaintext tokens")

    @contextlib.asynccontextmanager
    async def lifespan(server):
        # Single writer across processes, not merely per-interpreter locks.
        with (pool.state_dir / "manager.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stop = asyncio.Event()

            async def reconcile():
                while not stop.is_set():
                    try:
                        await asyncio.to_thread(pool.tick)
                    except Exception:
                        logging.exception("Reconciliation failed; retain allocations and retry the next bounded tick")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=interval)
                    except TimeoutError:
                        pass

            worker = asyncio.create_task(reconcile())
            try:
                yield {}
            finally:
                stop.set()
                await worker

    mcp = MCPServer("vaws-coordinator", lifespan=lifespan,
                    instructions="Reuse prepared runtimes. Stage edits before execution; pin snapshots, preflight and activate each run. Poll events for cooperative messages. Never infer release from a reply or timeout.")

    @mcp.tool()
    async def session_open(session_id: str, sources: dict[str, str]) -> dict:
        """Record logical task identity and actual local worktree paths; no machine allocation."""
        return await asyncio.to_thread(pool.session_open, PRINCIPAL.get(), session_id, sources)

    @mcp.tool()
    async def machine_catalog() -> dict:
        """Read the shared Git-common-dir machine directory; this is not a resource allocation."""
        return await asyncio.to_thread(pool.backend.catalog)

    @mcp.tool()
    async def runtime_register(runtime_id: str, spec: dict) -> dict:
        """Administrator: adopt an owned, idle, already prepared container after verification."""
        if not principals[PRINCIPAL.get()].get("admin", False):
            raise PermissionError("runtime registration requires an administrator")
        if "machine" in spec:
            spec = await asyncio.to_thread(pool.backend.resolve_registration, spec)
        return await asyncio.to_thread(pool.register, runtime_id, spec)

    @mcp.tool()
    async def runtime_checkout(session: str, profile_key: str, request_id: str, runtime_id: str = "") -> dict:
        """Exclusively bind a ready environment, without reserving NPUs or provisioning anything."""
        return await asyncio.to_thread(pool.checkout, PRINCIPAL.get(), session, profile_key, request_id, runtime_id)

    @mcp.tool()
    async def runtime_refresh(binding_id: str) -> dict:
        """Verify a newly prepared native bundle while no execution is pending; never builds it."""
        return await asyncio.to_thread(pool.refresh, PRINCIPAL.get(), binding_id)

    @mcp.tool()
    async def runtime_return(binding_id: str) -> dict:
        """Return a released runtime to quarantine; it must be cleaned and re-verified before reuse."""
        return await asyncio.to_thread(pool.return_runtime, PRINCIPAL.get(), binding_id)

    @mcp.tool()
    async def execution_request(binding_id: str, request_id: str, snapshots: dict[str, str],
                                expected_build_key: str, devices: list[int], npu_count: int = 0,
                                priority: int = 0, queue_seconds: int = 1800) -> dict:
        """Pin synchronized source and request physical cards; supply devices OR npu_count."""
        return await asyncio.to_thread(pool.request_run, PRINCIPAL.get(), binding_id, request_id,
                                       snapshots, expected_build_key, devices, npu_count, priority, queue_seconds)

    @mcp.tool()
    async def execution_control(run_id: str, action: str, pid: int = 0) -> dict:
        """poll/preflight/activate/heartbeat/release/cancel. Preflight immediately before launch; activate with its PID. Release only after stopping your workers."""
        return await asyncio.to_thread(pool.control, PRINCIPAL.get(), run_id, action, pid)

    @mcp.tool()
    async def coordinator_status() -> dict:
        """Read this principal's persisted sessions, bindings and executions."""
        return await asyncio.to_thread(pool.status, PRINCIPAL.get())

    @mcp.tool()
    async def coordination_peers() -> dict:
        """List cooperative peers' run ids and physical allocations, without endpoints/source paths."""
        return {"peers": await asyncio.to_thread(pool.peers)}

    @mcp.tool()
    async def coordination_message(target_run: str, text: str) -> dict:
        """Ask another run's owner to yield or coordinate. Delivery never releases its resources."""
        return await asyncio.to_thread(pool.message, PRINCIPAL.get(), target_run, text)

    @mcp.tool()
    async def coordination_reply(cursor: int, text: str) -> dict:
        """Reply to a received message. Acceptance is not a hardware-release acknowledgement."""
        return await asyncio.to_thread(pool.reply, PRINCIPAL.get(), cursor, text)

    @mcp.tool()
    async def coordination_events(after: int = 0, limit: int = 100) -> dict:
        """Poll durable events by cursor. This does not wake a paused AI client."""
        return await asyncio.to_thread(pool.events, PRINCIPAL.get(), after, limit)

    security = TransportSecuritySettings(
        allowed_hosts=allowed_hosts or ["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)

    async def authenticated(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"")
        token = auth[7:] if auth.startswith(b"Bearer ") else b""
        token_hash = hashlib.sha256(token).hexdigest()
        owner = next((name for name, config in principals.items() if token and hmac.compare_digest(config["sha256"], token_hash)), None)
        if owner is None:
            return await JSONResponse({"error": "bearer authentication required"}, status_code=401)(scope, receive, send)
        handle = PRINCIPAL.set(owner)
        try:
            return await app(scope, receive, send)
        finally:
            PRINCIPAL.reset(handle)

    return authenticated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=shared_workspace_root(ROOT) / ".vaws-local/coordinator")
    parser.add_argument("--access-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.access_file.stat().st_mode & 0o077:
        parser.error("access file must be private (chmod 600)")
    from backend import RemoteBackend
    import uvicorn
    app = create_app(RuntimePool(args.state_dir, RemoteBackend()), json.loads(args.access_file.read_text()))
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
