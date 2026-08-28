"""Actual authenticated HTTP MCP calls; no mocked JSON-RPC dispatcher."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx2
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from test_coordinator import Backend, RuntimePool, runtime_spec, ROOT
from server import create_app


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdlib_task_client_initializes_and_calls_the_actual_mcp_server(self):
        from vaws_task_client import CoordinatorClient

        directory = self.root / "local/agent-sessions"
        directory.mkdir(parents=True)
        token = directory.parent / "token"
        token.write_text("alice-secret-test")
        token.chmod(0o600)
        (directory.parent / "coordinator-client.json").write_text(json.dumps({"url": self.url, "token_file": str(token)}))

        def invoke():
            client = CoordinatorClient(directory)
            first = client.call("session_open", session_id="local-task", sources={"va": "/actual/worktree"})
            second = client.call("session_open", session_id="local-task", sources={"va": "/actual/worktree"})
            return first, second

        with mock.patch.dict("os.environ", {}, clear=True):
            first, second = await asyncio.to_thread(invoke)
        self.assertEqual(first["id"], second["id"])

    async def test_backend_import_does_not_shadow_the_official_mcp_sdk(self):
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c",
            "import sys; sys.path.insert(0, " + repr(str(ROOT / ".agents/coordinator")) + "); import backend; from mcp.server import MCPServer"],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = Backend(self.root / "host")
        self.access = {"principals": {name: {"sha256": hashlib.sha256((name + "-secret-test").encode()).hexdigest(), "admin": name == "alice"}
                                       for name in ["alice", "bob"]}}
        await self.start_server()

    async def start_server(self):
        self.pool = RuntimePool(self.root / "manager", self.backend)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.pool, self.access, interval=0.05), log_level="error", lifespan="on"))
        self.task = asyncio.create_task(self.server.serve(sockets=[listener]))
        for _ in range(200):
            if self.server.started:
                return
            if self.task.done():
                await self.task
                self.fail("HTTP server exited before readiness")
            await asyncio.sleep(0.01)
        self.fail("HTTP server did not start")

    async def stop_server(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 15)

    async def asyncTearDown(self):
        await self.stop_server()
        self.temp.cleanup()

    @contextlib.asynccontextmanager
    async def client(self, owner):
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + owner + "-secret-test"}) as http:
            async with streamable_http_client(self.url, http_client=http) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    yield client

    async def call(self, client, name, args=None, *, error=False):
        result = (await client.call_tool(name, args or {})).model_dump(by_alias=True)
        self.assertEqual(bool(result.get("isError")), error, result)
        if error:
            return result
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    async def test_two_clients_checkout_queue_yield_and_restart(self):
        async with self.client("alice") as alice, self.client("bob") as bob:
            names = {tool.name for tool in (await alice.list_tools()).tools}
            self.assertIn("runtime_checkout", names)
            await self.call(bob, "runtime_register", {"runtime_id": "runtime-a", "spec": runtime_spec(1)}, error=True)
            await self.call(bob, "runtime_drain", {"runtime_id": "runtime-a"}, error=True)
            for index in (1, 2):
                await self.call(alice, "runtime_register", {"runtime_id": "runtime-" + str(index), "spec": runtime_spec(index)})
            a = await self.call(alice, "session_open", {"session_id": "task-a", "sources": {"va": "/clients/clone-a/va"}})
            b = await self.call(bob, "session_open", {"session_id": "task-b", "sources": {"va": "/clients/linked-b/va"}})
            a = await self.call(alice, "runtime_checkout", {"session": a["id"], "profile_key": "profile-a", "request_id": "checkout"})
            b = await self.call(bob, "runtime_checkout", {"session": b["id"], "profile_key": "profile-a", "request_id": "checkout"})
            self.assertNotEqual(a["runtime_id"], b["runtime_id"])
            args = {"request_id": "run", "snapshots": {"vllm": "a" * 40, "vllm-ascend": "b" * 40}, "expected_build_key": "native-a", "devices": [0]}
            arun = await self.call(alice, "execution_request", {**args, "binding_id": a["id"]})
            brun = await self.call(bob, "execution_request", {**args, "binding_id": b["id"]})
            self.assertEqual(arun["state"], "granted")
            self.assertEqual(brun["state"], "queued")
            await self.call(bob, "execution_control", {"run_id": arun["id"], "action": "release"}, error=True)
            event = await self.call(bob, "coordination_message", {"target_run": arun["id"], "text": "Please yield when done"})
            await self.call(alice, "coordination_reply", {"cursor": event["cursor"], "text": "Agreed after my run"})
            self.assertEqual((await self.call(bob, "coordinator_status"))["runs"][0]["state"], "queued")
            await self.call(alice, "execution_control", {"run_id": arun["id"], "action": "release"})
            for _ in range(100):
                state = await self.call(bob, "coordinator_status")
                if state["runs"][0]["state"] == "granted":
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(state["runs"][0]["state"], "granted")
            events = await self.call(bob, "coordination_events")
        await self.stop_server()
        await self.start_server()
        async with self.client("bob") as bob:
            self.assertEqual((await self.call(bob, "coordinator_status"))["bindings"][0]["id"], b["id"])
            self.assertEqual((await self.call(bob, "coordination_events", {"after": events["cursor"]}))["events"], [])

    async def test_auth_and_dns_rebinding_protection(self):
        async with httpx2.AsyncClient() as http:
            self.assertEqual((await http.post(self.url, json={})).status_code, 401)
            headers = {"Authorization": "Bearer alice-secret-test", "Host": "evil.example", "Accept": "application/json, text/event-stream"}
            self.assertEqual((await http.post(self.url, headers=headers, json={})).status_code, 421)
            headers["Host"] = self.url.split("/")[2]
            headers["Origin"] = "https://evil.example"
            self.assertEqual((await http.post(self.url, headers=headers, json={})).status_code, 403)


if __name__ == "__main__":
    unittest.main()
