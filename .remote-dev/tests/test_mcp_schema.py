from __future__ import annotations

import importlib
import os
import sys
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPO_ROOT = ROOT.parent

import core.state_store as state_store  # noqa: E402
import mcp.tools as mcp_tools  # noqa: E402
from core.endpoint import Endpoint  # noqa: E402
from core.ssh_transport import RemoteCompleted  # noqa: E402
from mcp.schemas import ALIASES, ENDPOINT_SELECTOR_DESCRIPTION, TOOL_SCHEMAS  # noqa: E402
from mcp.tools import list_resources, list_tools, read_resource  # noqa: E402


class McpSchemaTests(unittest.TestCase):
    def test_all_expected_tools_are_listed(self) -> None:
        names = {tool["name"] for tool in list_tools()}
        self.assertEqual(names, set(ALIASES))
        self.assertEqual(set(ALIASES.values()), set(TOOL_SCHEMAS))
        for expected in (
            "remote.read",
            "remote.write",
            "remote.edit",
            "remote.multi_edit",
            "remote.bash",
            "remote.glob",
            "remote.grep",
            "remote.ls",
            "remote.monitor",
            "remote.apply_patch",
            "remote.job_status",
            "remote.job_tail",
            "remote.job_stop",
            "remote.artifact_manifest",
            "remote.artifact_pull",
            "remote.artifact_push",
            "remote.context_snapshot",
            "remote.probe",
        ):
            wire_name = expected.replace(".", "_")
            self.assertIn(wire_name, names)
            self.assertEqual(
                next(tool for tool in list_tools() if tool["name"] == wire_name)["inputSchema"],
                TOOL_SCHEMAS[expected],
            )

    def test_wire_names_are_portable_and_unique(self) -> None:
        names = [tool["name"] for tool in list_tools()]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, r"^[A-Za-z0-9_-]{1,64}$")

    def test_cursor_entry_uses_the_shared_server_and_environment(self) -> None:
        shared = json.loads((REPO_ROOT / ".mcp.json").read_text())["mcpServers"]["remote-dev"]
        cursor = json.loads((REPO_ROOT / ".cursor" / "mcp.json").read_text())["mcpServers"]["remote-dev"]
        self.assertEqual(cursor["type"], "stdio")
        self.assertEqual(cursor["command"], shared["command"])
        self.assertEqual(cursor["env"], shared["env"])
        self.assertEqual(cursor["args"], shared["args"])
        self.assertTrue((REPO_ROOT / shared["args"][0]).is_file())

    def test_underscore_aliases_map_to_canonical_names(self) -> None:
        self.assertEqual(ALIASES["remote_read"], "remote.read")
        self.assertIn("remote.bash", TOOL_SCHEMAS)

    def test_normal_tools_describe_endpoint_selector_requirement(self) -> None:
        job_tools = {"remote.job_status", "remote.job_tail", "remote.job_stop"}
        for name, schema in TOOL_SCHEMAS.items():
            if name in job_tools:
                self.assertNotIn(ENDPOINT_SELECTOR_DESCRIPTION, schema.get("description", ""))
            else:
                self.assertIn(ENDPOINT_SELECTOR_DESCRIPTION, schema.get("description", ""), name)

    def test_model_facing_schemas_use_portable_object_subset(self) -> None:
        def check(node: dict, path: str) -> None:
            for keyword in ("anyOf", "oneOf", "allOf", "$ref"):
                self.assertNotIn(keyword, node, path)
            self.assertIsInstance(node.get("type"), str, path)
            properties = node.get("properties", {})
            self.assertLessEqual(set(node.get("required", [])), set(properties), path)
            for key, child in properties.items():
                check(child, f"{path}.{key}")
            if node["type"] == "array":
                self.assertIsInstance(node.get("items"), dict, path)
                check(node["items"], f"{path}[]")
            if isinstance(node.get("additionalProperties"), dict):
                check(node["additionalProperties"], f"{path}.*")

        for tool in list_tools():
            with self.subTest(tool=tool["name"]):
                self.assertEqual(tool["inputSchema"]["type"], "object")
                check(tool["inputSchema"], tool["name"])

    def test_patch_schema_keeps_both_payload_forms(self) -> None:
        schema = TOOL_SCHEMAS["remote.apply_patch"]
        self.assertIn("patch or command", schema["description"])
        self.assertIn("patch takes precedence", schema["description"])
        for field in ("patch", "command"):
            self.assertEqual(schema["properties"][field]["type"], "string")
            self.assertNotIn(field, schema["required"])

    def test_multi_edit_items_expose_fields_to_strict_generators(self) -> None:
        item = TOOL_SCHEMAS["remote.multi_edit"]["properties"]["edits"]["items"]
        self.assertEqual(item["required"], ["old_string"])
        self.assertEqual(item["properties"]["new_string"]["default"], "")
        self.assertEqual(set(item["properties"]), {"old_string", "new_string", "replace_all"})

    def test_missing_endpoint_is_rejected_before_tool_execution(self) -> None:
        from core.errors import EndpointError

        with patch.object(mcp_tools, "remote_apply_patch") as execute:
            with self.assertRaises(EndpointError):
                mcp_tools.call_tool("remote.apply_patch", {"patch": "test"})
            execute.assert_not_called()

    def test_missing_patch_is_rejected_before_remote_execution(self) -> None:
        import core.patch_ops as patch_ops

        endpoint = {"host": "example.invalid", "port": 22, "root": "/tmp", "cwd": "/tmp"}
        with patch.object(patch_ops, "run_remote_python") as run_python, patch.object(patch_ops, "run_script") as run_shell:
            for name in ("remote.apply_patch", "remote_apply_patch"):
                result = mcp_tools.call_tool(name, endpoint)["result"]
                self.assertEqual(result["status"], "patch_required")
                self.assertEqual(result["outcome"], "needs_input")
            run_python.assert_not_called()
            run_shell.assert_not_called()

    def test_patch_and_legacy_command_are_forwarded_without_renaming(self) -> None:
        endpoint = {"host": "example.invalid", "port": 22, "root": "/tmp", "cwd": "/tmp"}
        for name in ("remote.apply_patch", "remote_apply_patch"):
            for field in ("patch", "command"):
                with self.subTest(name=name, field=field), patch.object(mcp_tools, "remote_apply_patch", return_value={}) as execute:
                    mcp_tools.call_tool(name, {**endpoint, field: "payload"})
                    self.assertEqual(execute.call_args.kwargs[field], "payload")

    def test_resources_include_endpoint_index(self) -> None:
        resources = {resource["uri"] for resource in list_resources()}
        self.assertIn("remote://endpoints", resources)
        content = read_resource("remote://endpoints")
        self.assertEqual(content["mimeType"], "application/json")
        self.assertIn("endpoints", json.loads(content["text"]))

    def test_resources_include_and_read_job_resources(self) -> None:
        original_state_root = state_store.substrate_root
        original_run_script = mcp_tools.run_script
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                endpoint = Endpoint(host="127.0.0.1", port=46000, root="/vllm-workspace")
                state_store.ensure_endpoint_state(endpoint)
                job_id = "job-abc123"
                record = {
                    "schema_version": "remote-dev.job.v1",
                    "job_id": job_id,
                    "target": endpoint.to_result_target(),
                    "remote_dir": f"{endpoint.root}/.remote-dev/jobs/{job_id}",
                    "started_at": "2026-05-25T00:00:00Z",
                }
                state_store.atomic_write_json(state_store.job_record_path(endpoint, job_id), record)
                mcp_tools.run_script = lambda *_args, **_kwargs: RemoteCompleted(0, "log\n", "")  # type: ignore[assignment]

                base = f"remote://endpoint/{endpoint.endpoint_id}/job/{job_id}"
                resources = {resource["uri"] for resource in list_resources()}
                self.assertIn(base + "/status", resources)
                self.assertIn(base + "/stdout", resources)
                self.assertIn(base + "/stderr", resources)

                status = read_resource(base + "/status")
                self.assertEqual(json.loads(status["text"])["job_id"], job_id)
                stdout = read_resource(base + "/stdout")
                self.assertEqual(stdout["mimeType"], "text/plain")
                self.assertEqual(stdout["text"], "log\n")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            mcp_tools.run_script = original_run_script  # type: ignore[assignment]

    def test_resources_include_and_read_artifact_manifest(self) -> None:
        original_state_root = state_store.substrate_root
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                endpoint = Endpoint(host="127.0.0.1", port=46000, root="/vllm-workspace")
                state_store.ensure_endpoint_state(endpoint)
                manifest = {
                    "schema_version": "remote-dev.artifact_manifest.v1",
                    "status": "ok",
                    "endpoint_id": endpoint.endpoint_id,
                    "file_count": 0,
                    "files": [],
                }
                artifact_id = "artifact-abc123"
                manifest_path = state_store.artifacts_dir(endpoint.endpoint_id) / artifact_id / "manifest.json"
                state_store.atomic_write_json(manifest_path, manifest)

                uri = f"remote://endpoint/{endpoint.endpoint_id}/artifacts/{artifact_id}/manifest"
                resources = {resource["uri"] for resource in list_resources()}
                self.assertIn(uri, resources)
                content = read_resource(uri)
                self.assertEqual(content["mimeType"], "application/json")
                self.assertEqual(json.loads(content["text"])["schema_version"], "remote-dev.artifact_manifest.v1")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]

    def test_context_snapshot_can_skip_live_probe(self) -> None:
        from mcp.tools import call_tool

        payload = call_tool(
            "remote.context_snapshot",
            {
                "host": "example.invalid",
                "port": 22,
                "root": "/vllm-workspace",
                "live_probe": False,
            },
        )
        self.assertEqual(payload["result"]["outcome"], "success")
        self.assertEqual(payload["result"]["tool"], "remote.context_snapshot")

    def test_server_supports_content_length_framing(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        encoded = json.dumps(request, separators=(",", ":")).encode("utf-8")
        framed = b"Content-Length: " + str(len(encoded)).encode("ascii") + b"\r\n\r\n" + encoded
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / ".remote-dev" / "mcp" / "server.py")],
            input=framed,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        header, body = proc.stdout.split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Length:", header)
        response = json.loads(body.decode("utf-8"))
        self.assertEqual(response["id"], 1)
        self.assertIn("tools", response["result"])

    def test_server_json_lines_lists_the_same_portable_schemas(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        proc = subprocess.run(
            [sys.executable, str(ROOT / "mcp" / "server.py")],
            input=json.dumps(request) + "\n", capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["result"]["tools"], list_tools())

    def test_mcp_server_sets_process_level_ledger_scope(self) -> None:
        from core.state_store import resolve_ledger_scope

        original = os.environ.pop("REMOTE_DEV_SESSION_ID", None)
        try:
            import mcp.server as mcp_server

            importlib.reload(mcp_server)
            scope = resolve_ledger_scope()
            self.assertTrue(scope.startswith("mcp-"))
            self.assertNotEqual(scope, "default")
        finally:
            if original is None:
                os.environ.pop("REMOTE_DEV_SESSION_ID", None)
            else:
                os.environ["REMOTE_DEV_SESSION_ID"] = original


if __name__ == "__main__":
    unittest.main()
