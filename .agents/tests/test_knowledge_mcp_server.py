"""stdio handshake for the installed vaws-knowledge MCP server."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))

from vaws_knowledge_service import knowledge_server_env  # noqa: E402

TOOLS = {"knowledge_query", "knowledge_explain", "knowledge_capture",
         "experience_query", "experience_explain", "experience_capture"}


def _rpc(method: str, request_id: int, params: dict | None = None) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _read_response(proc: subprocess.Popen[bytes]) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if line.startswith(b"Content-Length:"):
        length = int(line.split(b":", 1)[1].strip())
        blank = proc.stdout.readline()
        del blank
        body = proc.stdout.read(length)
        return json.loads(body.decode("utf-8"))
    return json.loads(line.decode("utf-8"))


class KnowledgeMcpHandshakeTest(unittest.TestCase):
    def test_initialize_lists_both_stores_tools(self) -> None:
        env = {
            **dict(__import__("os").environ),
            **knowledge_server_env(ROOT),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "vaws_knowledge.server.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            env=env,
        )
        try:
            assert proc.stdin is not None
            for message in (
                _rpc("initialize", 1, {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test"}}),
                _rpc("tools/list", 2),
            ):
                proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            proc.stdin.flush()
            initialized = _read_response(proc)
            listed = _read_response(proc)
            self.assertEqual(initialized["id"], 1)
            self.assertIn("result", initialized)
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertEqual(names, TOOLS)
        finally:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
