"""Local task identity, actual worktree binding and client hook contracts.

Run alongside the session-management suite in the remote CPU test environment.
These tests do not stand in for native-client acceptance.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_agent_session import AgentSessions, load_context
from vaws_task_client import TaskClient


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hooks = module_at("native_session_hooks", ROOT / ".agents/hooks/vaws_session.py")
setup = module_at("native_session_setup", ROOT / ".agents/scripts/vaws_client_setup.py")


class AgentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var is a /private/var symlink; resolve once so path equality
        # checks against resolved hook/setup outputs hold on the dev machine.
        self.root = Path(self.temp.name).resolve()
        self.store = AgentSessions(self.root / "state")
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def attach(self, native="root-a", client="codex", **args):
        return self.store.attach(client, native, str(self.root), **args)

    def test_new_native_sessions_same_cwd_are_distinct_but_resume_keeps_task(self):
        first, second = self.attach(), self.attach("root-b")
        self.assertNotEqual(first["session"]["id"], second["session"]["id"])
        self.store.detach(first)
        resumed = self.attach()
        self.assertEqual(resumed["session"]["id"], first["session"]["id"])
        self.assertEqual(resumed["attachment"]["id"], first["attachment"]["id"])
        self.assertEqual(load_context(first["context_file"])["attachment"]["state"], "attached")

    def test_repeated_concurrent_start_hooks_cannot_duplicate_a_task(self):
        def attach(_):
            return AgentSessions(self.root / "state").attach("codex", "native", str(self.root))
        with ThreadPoolExecutor(3) as workers:
            contexts = list(workers.map(attach, range(9)))
        self.assertEqual(len({item["session"]["id"] for item in contexts}), 1)

    def test_child_cross_tool_and_explicit_association_share_task_only_when_requested(self):
        parent = self.attach()
        child = self.attach("child", client="claude", parent_context=parent["context_file"])
        explicit = self.attach("other", client="kimi", association=parent["context_file"])
        independent = self.attach("other", client="grok")
        self.assertEqual(child["session"]["id"], parent["session"]["id"])
        self.assertEqual(child["attachment"]["parent_id"], parent["attachment"]["id"])
        self.assertEqual(explicit["session"]["id"], parent["session"]["id"])
        self.assertIsNone(explicit["attachment"]["parent_id"])
        self.assertNotEqual(independent["session"]["id"], parent["session"]["id"])
        with self.assertRaisesRegex(ValueError, "another task"):
            self.attach("other", client="grok", association=parent["context_file"])
        other_registry = AgentSessions(self.root / "clone-b")
        with self.assertRaisesRegex(ValueError, "local registry"):
            other_registry.attach("codex", "from-clone-b", str(self.root), parent_context=parent["context_file"])

    def test_finish_detach_and_resume_need_no_network_and_preserve_worktrees(self):
        context = self.attach()
        sources = {}
        for name in ("vllm", "vllm-ascend"):
            source = self.root / name
            source.mkdir()
            subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
            (source / "code.py").write_text("pass\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "-m", "base"], check=True, capture_output=True)
            sources[name] = str(source)
        factory = mock.Mock(side_effect=AssertionError("must stay offline"))
        task = TaskClient(context["context_file"], client_factory=factory)
        task.sources(sources)
        row = task.store.execution(task.context, "planned", {"command": "not started"})
        self.store.detach(context)
        self.assertEqual(task.status()["session"]["state"], "open")
        self.assertEqual(task.finish()["state"], "finished")
        self.assertTrue(all(Path(path).is_dir() for path in sources.values()))
        self.assertEqual(self.attach()["session"]["id"], context["session"]["id"])
        self.assertEqual(task.store.executions(context["session"]["id"])[0]["phase"], "cancelled")
        factory.assert_not_called()

    def test_hook_contracts_use_native_ids_and_resume_the_same_task(self):
        payloads = {
            "claude": {"hook_event_name": "SessionStart", "session_id": "native-claude"},
            "codex": {"hook_event_name": "SessionStart", "session_id": "native-codex"},
            "grok": {"hookEventName": "session_start", "sessionId": "native-grok"},
            "kimi": {"hook_event_name": "SessionStart", "session_id": "native-kimi"},
            "cursor": {"hook_event_name": "sessionStart", "conversation_id": "native-cursor", "cursor_version": "test"},
        }
        for client, payload in payloads.items():
            with self.subTest(client=client):
                payload["cwd"] = str(self.root)
                hooks.handle(client, payload, self.store)
                before = self.store.native_context(client, "native-" + client)
                hooks.handle(client, {**payload, "source": "resume"}, self.store)
                self.assertEqual(before["session"]["id"], self.store.native_context(client, "native-" + client)["session"]["id"])
        self.assertEqual(hooks.handle("claude", payloads["grok"], self.store), {})
        self.assertEqual(hooks.handle("cursor", payloads["grok"], self.store), {})
        with self.store.transaction() as db:
            self.assertEqual(len(self.store.rows(db, "session")), 5)

    def test_cursor_session_id_only_payload_attaches_and_grok_import_still_noops(self):
        # A genuine Cursor payload is discriminated by cursor_version, not by
        # the presence of conversation_id; Grok's Cursor-hook import carries
        # Grok's camelCase hookEventName and no cursor_version.
        payload = {"hook_event_name": "sessionStart", "sessionId": "cursor-session-id-only",
                   "cursor_version": "test", "cwd": str(self.root)}
        output = hooks.handle("cursor", payload, self.store)
        context = self.store.native_context("cursor", "cursor-session-id-only")
        self.assertIn("context_file", context)
        self.assertTrue(output)
        grok_import = {"hookEventName": "session_start", "sessionId": "grok-via-cursor", "cwd": str(self.root)}
        self.assertEqual(hooks.handle("cursor", grok_import, self.store), {})
        with self.assertRaises(ValueError):
            self.store.native_context("cursor", "grok-via-cursor")

    def test_kimi_hook_stays_silent_outside_project_and_on_errors(self):
        script = ROOT / ".agents/hooks/vaws_session.py"
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "kimi-outside",
                   "cwd": str(self.root), "prompt": "hello"}
        for args, stdin in (
            (["--client", "kimi", "--project", str(self.root / "elsewhere")], payload),
            (["--client", "kimi"], {**payload, "session_id": ""}),
        ):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(script), *args], input=json.dumps(stdin),
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0)
                self.assertNotIn("{}", result.stdout)
                self.assertEqual(result.stdout.strip(), "")

    def test_pretool_hook_injects_context_and_subagent_detach_does_not_end_root(self):
        parent = self.attach()
        child = {"hook_event_name": "SubagentStart", "session_id": "root-a", "agent_id": "child-1", "cwd": str(self.root)}
        hooks.handle("codex", child, self.store)
        context = self.store.native_context("codex", "root-a", "child-1")
        self.assertEqual(context["session"]["id"], parent["session"]["id"])
        output = hooks.handle("codex", {**child, "hook_event_name": "PreToolUse", "tool_name": "mcp__remote_dev__vaws_session", "tool_input": {}}, self.store)
        self.assertEqual(output["hookSpecificOutput"]["updatedInput"]["context_file"], context["context_file"])
        hooks.handle("codex", {**child, "hook_event_name": "SubagentStop"}, self.store)
        self.assertEqual(self.store.context(parent["attachment"]["id"])["attachment"]["state"], "attached")

    def test_grok_use_tool_dispatcher_injects_nested_context(self):
        context = self.attach(client="grok", native="grok-native")
        payload = {
            "hookEventName": "pre_tool_use",
            "sessionId": "grok-native",
            "cwd": str(self.root),
            "toolName": "use_tool",
            "toolInput": {
                "tool_name": "remote-dev__vaws_session",
                "tool_input": {"sources": {"workspace": str(self.root)}},
            },
        }
        output = hooks.handle("grok", payload, self.store)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["tool_name"], "remote-dev__vaws_session")
        self.assertEqual(updated["tool_input"]["context_file"], context["context_file"])
        self.assertNotIn("context_file", updated)

    def test_grok_qualified_hook_name_preserves_use_tool_envelope(self):
        context = self.attach(client="grok", native="grok-qualified")
        payload = {
            "hookEventName": "pre_tool_use",
            "sessionId": "grok-qualified",
            "cwd": str(self.root),
            "toolName": "remote-dev__vaws_session",
            "toolInput": {
                "tool_name": "remote-dev__vaws_session",
                "tool_input": {"sources": {"workspace": str(self.root)}},
            },
        }
        output = hooks.handle("grok", payload, self.store)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["tool_name"], "remote-dev__vaws_session")
        self.assertEqual(updated["tool_input"]["context_file"], context["context_file"])
        self.assertNotIn("context_file", updated)

    def test_client_setup_preserves_user_policy_and_does_not_grant_trust(self):
        settings = self.root / ".claude/settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"deny": ["Bash(ssh *)"]}, "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "my-hook"}]}]}}))
        files = setup.configuration("claude", self.root)
        value = json.loads(files[settings])
        self.assertEqual(value["permissions"], {"deny": ["Bash(ssh *)"]})
        self.assertEqual(len(value["hooks"]["SessionStart"]), 2)
        settings.write_text(files[settings])
        self.assertEqual(setup.configuration("claude", self.root)[settings], files[settings])
        kimi = self.root / "kimi-private.toml"
        kimi.write_text('default_model = "existing"\n')
        import tomllib
        result = tomllib.loads(setup.configuration("kimi", self.root, kimi_config=kimi)[kimi])
        self.assertEqual(result["default_model"], "existing")
        self.assertTrue(all("--project" in item["command"] for item in result["hooks"]))

    def test_lost_launch_reply_is_reconciled_without_syncing_running_sources(self):
        context = self.attach()
        with self.store.transaction() as db:
            session = self.store.get(db, "session", context["session"]["id"])
            session["sources"] = {name: {"path": str(self.root / name)} for name in ("vllm", "vllm-ascend")}
            self.store.put(db, "session", session)
        job = {}
        calls = []

        def call(name, **args):
            calls.append(name)
            if name == "session_open":
                return {"id": "remote-session"}
            if name == "runtime_checkout":
                return {"id": "binding", "build_key": "native"}
            if name == "managed_execution_start":
                job.update(id="managed-job", binding_id="binding", request={"request_id": args["request_id"]}, state="running")
                raise TimeoutError("reply lost after creation")
            if name == "coordinator_status":
                return {"jobs": [job]}
            if name == "managed_execution_control":
                return job
            raise AssertionError(name)

        client = mock.Mock()
        client.call.side_effect = call
        task = TaskClient(context["context_file"], client_factory=lambda _: client)
        with mock.patch.object(task, "_sync", return_value={"vllm": "a" * 40, "vllm-ascend": "b" * 40}) as sync:
            with self.assertRaises(TimeoutError):
                task.run("one-run", "command", profile_key="exact")
            execution = task.store.executions(context["session"]["id"])[0]
            self.assertEqual(task.observe(execution["id"])["state"], "running")
            self.assertEqual(task.run("one-run", "command", profile_key="exact")["state"], "running")
            sync.assert_called_once()
        self.assertEqual(calls.count("managed_execution_start"), 1)


if __name__ == "__main__":
    unittest.main()
