"""Official MCP SDK against the consumed task server subprocess.

Local-only: no manager, no remote transport, no network. The launcher execs the
installed vaws-coordinator package. Skip when the official SDK or the
package is missing; this file does not install dependencies.
"""
from __future__ import annotations

import asyncio
import getpass
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
TESTS = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
from client_setup_fixtures import native_task_entry

PACKAGE_PRESENT = importlib.util.find_spec("vaws_coordinator") is not None
if PACKAGE_PRESENT:
    from vaws_coordinator.agent_session import AgentSessions, load_context
    from vaws_coordinator.ready_runtime import safe_id
try:
    import importlib.metadata
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    SDK_VERSION = importlib.metadata.version("mcp")
except Exception:  # noqa: BLE001 - skipUnless needs a boolean
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None
    SDK_VERSION = None


def have_sdk() -> bool:
    return ClientSession is not None and SDK_VERSION == "1.30.0"


@unittest.skipUnless(PACKAGE_PRESENT, "vaws-coordinator is not installed; run `uv sync`")
@unittest.skipUnless(have_sdk(), "official MCP SDK 1.30.0 is not installed")
class OfficialStdioTests(unittest.TestCase):
    def test_local_only_task_lifecycle(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="coordinator-official-stdio-") as temporary:
            temp = Path(temporary).resolve()
            entry = native_task_entry(ROOT, temp / 'native-checkout', prepared=True)
            worktree = temp / "actual-business-worktree"
            worktree.mkdir()
            for arguments in (
                ["init"],
                ["config", "user.name", "Acceptance Fixture"],
                ["config", "user.email", "acceptance@example.invalid"],
                ["commit", "--allow-empty", "-m", "test-owned initial tree"],
            ):
                subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "-C", str(worktree), *arguments],
                    check=True,
                    capture_output=True,
                )
            marker = worktree / "preserve.txt"
            marker.write_text("test-owned worktree must survive finish\n")
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("VAWS_") and key != "PYTHONPATH"
            }
            environment["VAWS_AGENT_SESSIONS_DIR"] = str(temp / "registry")
            environment["VAWS_COORDINATOR_STATE_DIR"] = str(temp / "coordinator")
            environment["ACCEPTANCE_FORBID_COORDINATOR_SERVICE"] = "1"
            sitecustomize = temp / "sitecustomize.py"
            sitecustomize.write_text((TESTS / "guarded_local_entry.py").read_text(encoding="utf-8"), encoding="utf-8")
            environment["PYTHONPATH"] = str(temp)
            reports = []

            def attach(native, *, parent_context=""):
                guard = temp / ("attach-" + str(len(reports)) + ".json")
                child_env = {**environment, "ACCEPTANCE_GUARD_REPORT": str(guard)}
                command = [
                    sys.executable,
                    str(entry),
                    "attach",
                    "--client",
                    "codex",
                    "--native-session-id",
                    native,
                    "--cwd",
                    str(worktree),
                ]
                if parent_context:
                    command += ["--parent-context", parent_context]
                proc = subprocess.run(
                    command, env=child_env, cwd=temp, capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                reports.append(guard)
                return json.loads(proc.stdout)

            first = attach("acceptance-native-a")
            resumed = attach("acceptance-native-a")
            separate = attach("acceptance-native-b")
            child = attach("acceptance-native-child", parent_context=first["context_file"])
            task_a = first["session"]["id"]
            task_b = separate["session"]["id"]
            self.assertEqual(task_a, resumed["session"]["id"])
            self.assertEqual(task_a, child["session"]["id"])
            self.assertNotEqual(task_a, task_b)

            server_guard = temp / "task-server-guard.json"
            reports.append(server_guard)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(entry), "task-server"],
                cwd=str(temp),
                env={**environment, "ACCEPTANCE_GUARD_REPORT": str(server_guard)},
            )
            with (temp / "server-stderr.log").open("w+") as errors:
                async with stdio_client(parameters, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as client:
                        initialized = (await client.initialize()).model_dump(by_alias=True)
                        capability = initialized["capabilities"]["experimental"]["vaws-coordinator-task"]
                        self.assertIn("version", capability)
                        self.assertIn("host_protocol_schema_version", capability)
                        self.assertIsInstance(capability["version"], str)
                        names = [tool.name for tool in (await client.list_tools()).tools]
                        self.assertEqual(set(names), {"vaws_session", "vaws_run", "vaws_execution", "vaws_finish", "vaws_message"})

                        async def call(name, context=None, **arguments):
                            if context:
                                arguments["context_file"] = context
                            result = (await client.call_tool(name, arguments)).model_dump(by_alias=True)
                            structured = result["structuredContent"]
                            self.assertEqual(result["content"][0]["text"], structured["summary"])
                            return result, structured

                        opened, state = await call(
                            "vaws_session", first["context_file"], sources={"fixture": str(worktree)}
                        )
                        self.assertFalse(opened["isError"])
                        self.assertEqual(state["status"], "open")
                        self.assertEqual(
                            state["data"]["session"]["sources"]["fixture"]["path"],
                            str(worktree.resolve()),
                        )
                        missing, missing_state = await call("vaws_session")
                        self.assertTrue(missing["isError"])
                        self.assertEqual(missing_state["status"], "unavailable")

                        ran, run_state = await call(
                            "vaws_run",
                            first["context_file"],
                            command=" ",
                        )
                        self.assertTrue(ran["isError"])
                        self.assertEqual((run_state["outcome"], run_state["status"]), ("blocked", "unavailable"))
                        self.assertIn("command is required", run_state["summary"])
                        _, after_run = await call("vaws_session", first["context_file"])
                        self.assertEqual(after_run["data"]["executions"], [])

                        context_a = load_context(first["context_file"])
                        store = AgentSessions(Path(environment["VAWS_AGENT_SESSIONS_DIR"]))
                        owned = store.execution(
                            context_a,
                            "stdio-ownership-fixture",
                            {"command": "true"},
                        )
                        owned.update(
                            phase="cancelled",
                            admitted=False,
                            user=safe_id(getpass.getuser()),
                        )
                        store.save_execution(owned)
                        eid = owned["id"]
                        self.assertEqual(len(eid), 64)

                        _, after_fixture = await call("vaws_session", first["context_file"])
                        owned_ids = [
                            row.get("id") or row.get("execution_id")
                            for row in after_fixture["data"]["executions"]
                        ]
                        self.assertIn(eid, owned_ids)

                        foreign, rejected = await call(
                            "vaws_execution",
                            separate["context_file"],
                            execution_id=eid,
                        )
                        self.assertTrue(foreign["isError"], rejected)
                        self.assertIn("another VAWS task", rejected["summary"])

                        finished, terminal = await call("vaws_finish", first["context_file"])
                        self.assertFalse(finished["isError"], terminal)
                        self.assertEqual(terminal["status"], "finished")
                        self.assertTrue(terminal["data"]["worktrees_preserved"])
                        self.assertTrue(marker.read_text(encoding="utf-8").startswith("test-owned"))
                        _, neighbor = await call("vaws_session", separate["context_file"])
                        self.assertEqual(neighbor["status"], "open")
                        reopened = attach("acceptance-native-a")
                        self.assertEqual(reopened["session"]["id"], task_a)
                        self.assertEqual(reopened["session"]["state"], "open")

            self.assertFalse((temp / "coordinator/coordinator.ipc").exists())
            self.assertFalse((temp / "coordinator/daemon.log").exists())

            for path in reports:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["events"], [], path)


if __name__ == "__main__":
    unittest.main()
