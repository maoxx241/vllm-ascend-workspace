from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vaws_top_client import ClientError, VawsTopClient, format_mounts, format_npu, format_server  # noqa: E402


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


MCP = load_script("vaws-top-mcp.py")
PAYLOAD = {
    "source": "cache",
    "server": {"id": "s1", "name": "a3", "host": "10.0.0.8", "enabled": True, "tags": [], "status": "online"},
    "collected_at": 10, "age_seconds": 2,
    "summary": {"npu_count": 1, "busy_npu_count": 1, "idle_npu_count": 0, "aicore_percent": 72, "hbm_used_mb": 32768, "hbm_total_mb": 65536},
    "devices": [{"id": 0, "aicore_percent": 72, "hbm_used_mb": 32768, "hbm_total_mb": 65536, "busy": True, "process_count": 1, "containers": ["vllm"], "owners": ["wbj"]}],
}


class FakeClient:
    def npu(self, host, include_processes=False, detailed_processes=False, mode="cache", timeout=30):
        if host == "missing":
            raise ClientError("server not found: missing")
        return PAYLOAD

    def servers(self):
        return {"source": "cache", "servers": []}

    def server(self, host, mode="cache", include_processes=True, detailed_processes=False, timeout=30):
        return {**PAYLOAD, "system": {}, "storage": {"mounts": []}, "docker": {"containers": []}}

    def capacity(self, min_idle_npus=1, max_age_seconds=300, tags=None, include_disabled=False):
        return {"source": "cache", "requirements": {}, "candidates": []}


class AgentCliMcpTests(unittest.TestCase):
    def test_default_formatter_is_compact_and_decision_focused(self) -> None:
        output = format_npu(PAYLOAD)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn("10.0.0.8 online age=2s npu=1 busy=1", output)
        self.assertIn("0 busy util=72% hbm=32.0G/64.0G proc=1 owner=wbj", output)

    def test_mount_formatter_hides_virtual_and_container_filesystems(self) -> None:
        output = format_mounts({"storage": {"mounts": [
            {"target": "/", "source": "/dev/root", "fstype": "ext4", "used_percent": 50, "available_bytes": 1024 ** 3, "total_bytes": 2 * 1024 ** 3},
            {"target": "/data/weights", "source": "nfs:/models", "fstype": "nfs4", "weight_candidate": True},
            {"target": "/proc", "source": "proc", "fstype": "proc"},
            {"target": "/var/lib/docker/overlay/merged", "source": "overlay", "fstype": "overlay"},
        ]}})
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn("/data/weights", output)
        self.assertNotIn("/proc", output)

    def test_server_formatter_groups_same_owner_container_processes(self) -> None:
        process = {"pid": 11, "name": "Worker", "container": "vllm", "owners": ["q00946761"], "npu_memory_mb": 1024}
        payload = {
            **PAYLOAD, "system": {}, "storage": {"mounts": []},
            "devices": [
                {**PAYLOAD["devices"][0], "processes": [process]},
                {**PAYLOAD["devices"][0], "processes": [{**process, "pid": 12}]},
            ],
        }
        output = format_server(payload)
        self.assertIn("proc x2 npu_mem=2.0G container=vllm owner=q00946761 name=Worker pids=11,12", output)
        self.assertEqual(sum(line.startswith("proc ") for line in output.splitlines()), 1)

    def test_client_rejects_remote_endpoint_by_default(self) -> None:
        with self.assertRaises(ClientError):
            VawsTopClient("http://10.0.0.8:8789")
        VawsTopClient("http://localhost:9999")

    def test_client_explicitly_bypasses_environment_proxies(self) -> None:
        with mock.patch("vaws_top_client.build_opener") as build:
            VawsTopClient()
        proxy_handler = build.call_args.args[0]
        self.assertEqual(proxy_handler.proxies, {})

    def test_live_client_expands_http_timeout_to_collection_budget(self) -> None:
        client = VawsTopClient()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"server":{}}'
        with mock.patch.object(client.opener, "open", return_value=response) as opened:
            client.server("10.0.0.8", mode="live", timeout=30)
        self.assertEqual(opened.call_args.kwargs["timeout"], 32)

    def test_legacy_mcp_lists_and_calls_tools(self) -> None:
        listed = MCP.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, FakeClient())
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            ["npu_status", "server_status", "list_mounts", "find_npu_capacity", "list_npu_servers"],
        )
        called = MCP.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "npu_status", "arguments": {"host": "10.0.0.8"}},
        }, FakeClient())
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(called["result"]["structuredContent"]["source"], "cache")

    def test_modern_mcp_returns_result_type(self) -> None:
        request = {
            "jsonrpc": "2.0", "id": 3, "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
        }
        response = MCP.handle_request(request, FakeClient())
        self.assertEqual(response["result"]["resultType"], "complete")

    def test_mcp_stale_cache_is_actionable_without_refresh(self) -> None:
        request = {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "npu_status", "arguments": {"host": "10.0.0.8", "max_age_seconds": 1}},
        }
        response = MCP.handle_request(request, FakeClient())
        self.assertTrue(response["result"]["isError"])
        self.assertIn("stale snapshot", response["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
