"""Scaffold-side contract with the extracted vaws-coordinator.

Locator, launcher, client-setup preservation and the build-input pin run
without a coordinator checkout except tests marked ``requires_coordinator``.
Those use ``VAWS_COORDINATOR_ROOT``. Official MCP SDK coverage lives in
``test_coordinator_official_stdio.py``.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_coordinator as coordinator  # noqa: E402

CHECKOUT = coordinator.coordinator_root(required=False)
requires_coordinator = unittest.skipUnless(CHECKOUT, "no vaws-coordinator checkout (set VAWS_COORDINATOR_ROOT)")


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_fake_checkout(root: Path) -> Path:
    for relative in coordinator.REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def _init_git_checkout(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "dev"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fake"], cwd=root, check=True, capture_output=True)


class LocatorTests(unittest.TestCase):
    def test_env_root_must_look_like_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(coordinator.CoordinatorUnavailable) as ctx:
                coordinator.coordinator_root(env={coordinator.COORDINATOR_ROOT_ENV: tmp})
            self.assertIn("not a vaws-coordinator checkout", str(ctx.exception))
            self.assertIsNone(
                coordinator.coordinator_root(required=False, env={coordinator.COORDINATOR_ROOT_ENV: tmp})
            )

    def test_fake_checkout_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_fake_checkout(Path(tmp))
            with self.assertRaises(coordinator.CoordinatorUnavailable) as ctx:
                coordinator.coordinator_root(env={coordinator.COORDINATOR_ROOT_ENV: tmp})
            self.assertIn("not a vaws-coordinator checkout", str(ctx.exception))
            self.assertIn("bootstrap", str(ctx.exception))
            self.assertIsNone(
                coordinator.coordinator_root(required=False, env={coordinator.COORDINATOR_ROOT_ENV: tmp})
            )
            status = coordinator.checkout_status({coordinator.COORDINATOR_ROOT_ENV: tmp})
            self.assertEqual(status["state"], "not_git")
            self.assertEqual(status["root_source"], "env")
            self.assertIsNone(status["manager_state_dir_default"])

    def test_environment_fills_the_single_registry_and_host_queue(self) -> None:
        env = coordinator.coordinator_environment({})
        self.assertTrue(env["VAWS_AGENT_SESSIONS_DIR"].endswith("agent-sessions"))
        self.assertEqual(env["VAWS_HOST_QUEUE_MODULE"], str(LIB / "vaws_npu_coordination.py"))
        self.assertTrue(env["VAWS_PARITY_SCRIPT"].endswith("remote_code_parity.py"))
        self.assertNotIn("VAWS_COORDINATOR_STATE_DIR", env)

    def test_environment_keeps_caller_values(self) -> None:
        env = coordinator.coordinator_environment({
            "VAWS_AGENT_SESSIONS_DIR": "/tmp/explicit-registry",
            "VAWS_HOST_QUEUE_MODULE": "/tmp/host.py",
        })
        self.assertEqual(env["VAWS_AGENT_SESSIONS_DIR"], "/tmp/explicit-registry")
        self.assertEqual(env["VAWS_HOST_QUEUE_MODULE"], "/tmp/host.py")

    def test_relative_registry_path_uses_the_shared_workspace(self) -> None:
        from vaws_local_state import shared_workspace_root
        env = coordinator.coordinator_environment({"VAWS_AGENT_SESSIONS_DIR": ".vaws-local/agent-sessions"})
        self.assertEqual(
            env["VAWS_AGENT_SESSIONS_DIR"],
            str(shared_workspace_root(ROOT) / ".vaws-local" / "agent-sessions"),
        )

    def test_dependency_pin_names_the_accepted_main(self) -> None:
        pin = coordinator.load_dependency()
        self.assertEqual(pin["repository"], "vllm-ascend-workspace/vaws-coordinator")
        self.assertEqual(pin["commit"], "2e16e894e31a12d85a11117a2772031f30fdfebe")
        self.assertEqual(pin["tree"], "fc64eacacf16060446895e2fa0a23a1fe0d17b4e")
        self.assertEqual(pin["visibility"], "public")
        self.assertEqual(
            pin["pinned_mirrors"]["vaws_build_inputs"]["sha256"],
            "967adeb699e47de2e281581d576a69f6c85075385975e42916ead1ed198a2e09",
        )


class BuildInputPinTests(unittest.TestCase):
    def test_scaffold_copy_matches_the_recorded_digest(self) -> None:
        pin = coordinator.load_dependency()["pinned_mirrors"]["vaws_build_inputs"]
        digest = hashlib.sha256((ROOT / pin["scaffold_path"]).read_bytes()).hexdigest()
        self.assertEqual(digest, pin["sha256"])

    @requires_coordinator
    def test_scaffold_copy_matches_the_coordinator_checkout(self) -> None:
        pin = coordinator.load_dependency()["pinned_mirrors"]["vaws_build_inputs"]
        left = (ROOT / pin["scaffold_path"]).read_bytes()
        right = (CHECKOUT / pin["coordinator_path"]).read_bytes()
        self.assertEqual(left, right)


class LauncherTests(unittest.TestCase):
    def test_status_without_checkout_reports_missing_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, coordinator.COORDINATOR_ROOT_ENV: str(Path(tmp) / "absent")}
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "vaws.py"), "status"],
                capture_output=True, text=True, env=env, check=False,
            )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"], "missing")
        self.assertEqual(payload["root_source"], "env")
        self.assertIsNone(payload["manager_state_dir_default"])

    def test_task_server_and_task_ops_without_checkout_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, coordinator.COORDINATOR_ROOT_ENV: str(Path(tmp) / "absent")}
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "vaws.py"), "task-server"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("bootstrap", proc.stderr)
            for operation in ("attach", "session", "run", "execution", "finish"):
                argv = [sys.executable, str(SCRIPTS / "vaws.py"), operation]
                if operation == "attach":
                    argv += ["--client", "codex", "--native-session-id", "n1"]
                elif operation == "run":
                    argv += ["--request-id", "r1", "--command", "true"]
                elif operation == "execution":
                    argv += ["--execution-id", "e1"]
                else:
                    argv += ["--json", "{}"]
                child = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
                self.assertEqual(child.returncode, 1, (operation, child.stderr))
                self.assertNotIn("Traceback", child.stderr)
                payload = json.loads(child.stdout)["result"]
                self.assertEqual(payload["outcome"], "blocked")
                self.assertEqual(payload["status"], "unavailable")
                self.assertIn("vaws-coordinator", payload["summary"] + json.dumps(payload))

    def test_hook_without_checkout_does_not_write_a_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, coordinator.COORDINATOR_ROOT_ENV: str(Path(tmp) / "absent")}
            proc = subprocess.run(
                [sys.executable, str(ROOT / ".agents/hooks/vaws_session.py"), "--client", "codex"],
                input='{"hook_event_name":"SessionStart","session_id":"n1"}',
                capture_output=True, text=True, env=env, check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("unavailable", proc.stderr)
        self.assertFalse(list(Path(tmp).rglob("sessions.sqlite3")))

    def test_env_json_lists_owned_keys(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws.py"), "env", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("VAWS_AGENT_SESSIONS_DIR", payload)
        self.assertIn("VAWS_HOST_QUEUE_MODULE", payload)
        self.assertTrue(all(key.startswith("VAWS_") for key in payload))

    def test_help_matrix(self) -> None:
        for args in (["--help"], ["status", "--help"], ["bootstrap", "--help"]):
            with self.subTest(args=args):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / "vaws.py"), *args],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)


class ClientSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.setup = load_script("vaws_client_setup")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve() / "project"
        self.project.mkdir()
        patcher = mock.patch.dict(os.environ, {coordinator.COORDINATOR_ROOT_ENV: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_json_emits_both_launchers(self) -> None:
        files = self.setup.configuration("claude", self.project)
        servers = json.loads(files[self.project / ".mcp.json"])["mcpServers"]
        self.assertEqual(set(servers), {"remote-dev", "vaws-task"})
        self.assertEqual(servers["remote-dev"]["args"], [str(ROOT / ".agents/scripts/remote_dev.py"), "server"])
        self.assertEqual(servers["vaws-task"]["args"], [str(ROOT / ".agents/scripts/vaws.py"), "task-server"])
        self.assertEqual(servers["vaws-task"]["type"], "stdio")
        self.assertIn("VAWS_AGENT_SESSIONS_DIR", servers["vaws-task"]["env"])
        self.assertNotIn(coordinator.COORDINATOR_ROOT_ENV, servers["vaws-task"]["env"])
        hook = json.loads(files[self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("--agent-sessions-dir", hook)
        self.assertNotIn("--coordinator-root", hook)

    def test_task_only_skips_remote_dev(self) -> None:
        servers = json.loads(
            self.setup.configuration("claude", self.project, task_only=True)[self.project / ".mcp.json"]
        )["mcpServers"]
        self.assertEqual(list(servers), ["vaws-task"])
        grok = tomllib.loads(
            self.setup.configuration("grok", self.project, task_only=True)[self.project / ".grok/config.toml"]
        )
        self.assertEqual(list(grok["mcp_servers"]), ["vaws_task"])

    def test_json_preserves_hand_managed_remote_dev_command_args_type(self) -> None:
        path = self.project / ".mcp.json"
        old = {
            "user_top": "preserve",
            "mcpServers": {
                "remote-dev": {
                    "command": "user-command",
                    "args": ["user-argument"],
                    "type": "stdio",
                    "env": {"USER_SETTING": "fixture-value"},
                    "user_field": 17,
                },
                "other": {"command": "other-command"},
            },
        }
        path.write_text(json.dumps(old))
        plan = self.setup.build_plan("claude", self.project)
        data = json.loads(plan["files"][path])
        entry = data["mcpServers"]["remote-dev"]
        self.assertEqual(entry["command"], "user-command")
        self.assertEqual(entry["args"], ["user-argument"])
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(entry["user_field"], 17)
        self.assertEqual(entry["env"]["USER_SETTING"], "fixture-value")
        self.assertEqual(data["user_top"], "preserve")
        self.assertEqual(data["mcpServers"]["other"], {"command": "other-command"})
        self.assertIn("vaws-task", data["mcpServers"])
        self.assertTrue(any(note.get("reason") == "existing-named-server" for note in plan["notes"]))

    def test_json_setup_is_idempotent_on_fixtures(self) -> None:
        first = self.setup.configuration("claude", self.project)
        path = self.project / ".mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(first[path])
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(first[settings])
        second = self.setup.configuration("claude", self.project)
        self.assertEqual(second[path], first[path])
        self.assertEqual(second[settings], first[settings])

    def test_toml_preserves_existing_remote_dev_and_adds_task_server(self) -> None:
        config = self.project / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text(
            'user_top = "preserve"\n[mcp_servers.remote_dev]\ncommand = "user-command"\n'
            'args = ["user-argument"]\nuser_field = 17\n[mcp_servers.other]\ncommand = "other-command"\n'
        )
        files = self.setup.configuration("codex", self.project)
        data = tomllib.loads(files[config])
        self.assertEqual(data["user_top"], "preserve")
        self.assertEqual(data["mcp_servers"]["remote_dev"]["command"], "user-command")
        self.assertEqual(data["mcp_servers"]["remote_dev"]["args"], ["user-argument"])
        self.assertEqual(data["mcp_servers"]["other"], {"command": "other-command"})
        self.assertEqual(data["mcp_servers"]["vaws_task"]["args"][1], "task-server")
        config.write_text(files[config])
        self.assertNotIn(config, self.setup.configuration("codex", self.project))

    def test_stale_tool_prefix_permissions_are_reported_not_rewritten(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({
            "permissions": {"allow": ["mcp__remote-dev__vaws_session", "Bash(python3 *)"]},
            "hooks": {},
        }))
        plan = self.setup.build_plan("claude", self.project)
        text = plan["files"][settings]
        self.assertIn("mcp__remote-dev__vaws_session", text)
        self.assertTrue(any(note.get("reason") == "stale-tool-prefix-permission" for note in plan["notes"]))
        self.assertIn("mcp__remote-dev__vaws_session", json.loads(text)["permissions"]["allow"])

    def test_preview_does_not_write(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws_client_setup.py"), "--client", "claude", "--project", str(self.project)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"], "preview")
        self.assertFalse(payload["trust_granted"])
        self.assertFalse((self.project / ".mcp.json").exists())

    def test_apply_stays_inside_the_fixture_project(self) -> None:
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "vaws_client_setup.py"),
                "--client", "claude", "--project", str(self.project), "--apply",
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"], "configured")
        self.assertTrue((self.project / ".mcp.json").is_file())
        for item in payload["files"]:
            self.assertTrue(item["path"].startswith(str(self.project)))

    def test_generated_provider_and_hook_keep_explicit_root_and_registry(self) -> None:
        root = (self.project / "coord root").resolve()
        registry = (self.project / "reg dir").resolve()
        with mock.patch.dict(os.environ, {
            coordinator.COORDINATOR_ROOT_ENV: str(root),
            "VAWS_AGENT_SESSIONS_DIR": str(registry),
        }):
            plan = self.setup.build_plan("claude", self.project, task_only=True)
        server = json.loads(plan["files"][self.project / ".mcp.json"])["mcpServers"]["vaws-task"]
        self.assertEqual(server["env"][coordinator.COORDINATOR_ROOT_ENV], str(root))
        self.assertEqual(server["env"]["VAWS_AGENT_SESSIONS_DIR"], str(registry))
        hook = json.loads(plan["files"][self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        argv = shlex.split(hook)
        self.assertEqual(argv[argv.index("--coordinator-root") + 1], str(root))
        self.assertEqual(argv[argv.index("--agent-sessions-dir") + 1], str(registry))

    def test_existing_user_env_wins_over_setup_root(self) -> None:
        path = self.project / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": {"vaws-task": {
            "command": "user-command",
            "args": ["user-argument"],
            "env": {coordinator.COORDINATOR_ROOT_ENV: "/user/managed/root", "VAWS_AGENT_SESSIONS_DIR": "/user/managed/registry"},
            "user_field": 1,
        }}}))
        with mock.patch.dict(os.environ, {
            coordinator.COORDINATOR_ROOT_ENV: str(self.project / "setup-root"),
            "VAWS_AGENT_SESSIONS_DIR": str(self.project / "setup-registry"),
        }):
            plan = self.setup.build_plan("claude", self.project, task_only=True)
        server = json.loads(plan["files"][path])["mcpServers"]["vaws-task"]
        self.assertEqual(server["command"], "user-command")
        self.assertEqual(server["env"][coordinator.COORDINATOR_ROOT_ENV], "/user/managed/root")
        self.assertEqual(server["env"]["VAWS_AGENT_SESSIONS_DIR"], "/user/managed/registry")
        self.assertEqual(server["user_field"], 1)
        hook = json.loads(plan["files"][self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        argv = shlex.split(hook)
        self.assertEqual(argv[argv.index("--coordinator-root") + 1], "/user/managed/root")
        self.assertEqual(argv[argv.index("--agent-sessions-dir") + 1], "/user/managed/registry")

    def test_previously_generated_hook_is_replaced_once(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        old = shlex.join([
            sys.executable,
            str(ROOT / ".agents/hooks/vaws_session.py"),
            "--client", "claude",
            "--project", str(self.project),
        ])
        settings.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "my-hook"}]},
            {"hooks": [{"type": "command", "command": old, "timeout": 12}]},
        ]}}))
        with mock.patch.dict(os.environ, {coordinator.COORDINATOR_ROOT_ENV: str(self.project / "coord")}):
            first = self.setup.build_plan("claude", self.project)
            groups = json.loads(first["files"][settings])["hooks"]["SessionStart"]
            commands = [entry.get("command", "") for group in groups for entry in group.get("hooks", [group])]
            self.assertEqual(commands.count("my-hook"), 1)
            owned = [item for item in commands if "vaws_session.py" in item]
            self.assertEqual(len(owned), 1)
            self.assertIn("--coordinator-root", owned[0])
            self.assertNotEqual(owned[0], old)
            settings.write_text(first["files"][settings])
            second = self.setup.build_plan("claude", self.project)
            self.assertEqual(second["files"][settings], first["files"][settings])

    def test_foreign_same_basename_hook_is_preserved(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        foreign = shlex.join([
            sys.executable, "/user/custom/vaws_session.py",
            "--client", "claude", "--project", str(self.project),
        ])
        user = {"type": "command", "command": foreign, "timeout": 37, "user_metadata": "retain"}
        original = {
            "user_top_metadata": "retain",
            "hooks": {"SessionStart": [{
                "matcher": "*",
                "user_group_metadata": "retain",
                "hooks": [user],
            }]},
        }
        settings.write_text(json.dumps(original))
        plan = self.setup.build_plan("claude", self.project, task_only=True)
        after = json.loads(plan["files"][settings])
        self.assertEqual(after["user_top_metadata"], "retain")
        group = after["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "*")
        self.assertEqual(group["user_group_metadata"], "retain")
        self.assertIn(user, group["hooks"])
        owned = [
            entry for item in after["hooks"]["SessionStart"] for entry in item["hooks"]
            if self.setup.owned_hook_command(entry.get("command", ""), "claude", self.project)
        ]
        self.assertEqual(len(owned), 1)
        self.assertNotEqual(owned[0]["command"], foreign)

    def test_mixed_group_keeps_user_sibling_and_metadata(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        old = shlex.join([
            sys.executable, str(ROOT / ".agents/hooks/vaws_session.py"),
            "--client", "claude", "--project", str(self.project),
        ])
        user = {"type": "command", "command": "my-existing-audit-hook --record", "timeout": 37, "user_metadata": "retain"}
        settings.write_text(json.dumps({
            "user_top_metadata": "retain",
            "hooks": {"SessionStart": [{
                "matcher": "*",
                "user_group_metadata": "retain",
                "hooks": [{"type": "command", "command": old, "timeout": 12}, user],
            }]},
        }))
        with mock.patch.dict(os.environ, {coordinator.COORDINATOR_ROOT_ENV: str(self.project / "coord")}):
            plan = self.setup.build_plan("claude", self.project, task_only=True)
            after = json.loads(plan["files"][settings])
            self.assertEqual(after["user_top_metadata"], "retain")
            groups = after["hooks"]["SessionStart"]
            self.assertEqual(len(groups), 1)
            group = groups[0]
            self.assertEqual(group["matcher"], "*")
            self.assertEqual(group["user_group_metadata"], "retain")
            self.assertIn(user, group["hooks"])
            owned = [
                entry for entry in group["hooks"]
                if self.setup.owned_hook_command(entry.get("command", ""), "claude", self.project)
            ]
            self.assertEqual(len(owned), 1)
            self.assertIn("--coordinator-root", owned[0]["command"])
            self.assertNotEqual(owned[0]["command"], old)
            settings.write_text(plan["files"][settings])
            second = self.setup.build_plan("claude", self.project, task_only=True)
            self.assertEqual(second["files"][settings], plan["files"][settings])

    def test_wrapper_data_argument_is_not_owned(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        wrapper = shlex.join([
            sys.executable, str(self.project / "audit-wrapper.py"),
            "--hook", str(ROOT / ".agents/hooks/vaws_session.py"),
            "--client", "claude", "--project", str(self.project),
        ])
        user = {"type": "command", "command": wrapper, "timeout": 9, "user_metadata": "retain"}
        settings.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "UserPromptSubmit", "hooks": [user]}]}}))
        plan = self.setup.build_plan("claude", self.project, task_only=True)
        after = json.loads(plan["files"][settings])
        group = after["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "UserPromptSubmit")
        self.assertIn(user, group["hooks"])
        owned = [
            entry for item in after["hooks"]["SessionStart"] for entry in item.get("hooks", [item])
            if self.setup.owned_hook_command(entry.get("command", ""), "claude", self.project)
        ]
        self.assertEqual(len(owned), 1)

    def test_cursor_flat_list_replaces_only_owned_command(self) -> None:
        hooks = self.project / ".cursor/hooks.json"
        hooks.parent.mkdir()
        old = shlex.join([
            sys.executable, str(ROOT / ".agents/hooks/vaws_session.py"),
            "--client", "cursor", "--project", str(self.project),
        ])
        hooks.write_text(json.dumps({
            "version": 1,
            "hooks": {"sessionStart": [
                {"command": old, "user_field": "owned-meta"},
                {"command": "user-cursor-hook", "user_field": "retain"},
            ]},
        }))
        plan = self.setup.build_plan("cursor", self.project, task_only=True)
        after = json.loads(plan["files"][hooks])
        groups = after["hooks"]["sessionStart"]
        user = [item for item in groups if item.get("command") == "user-cursor-hook"]
        self.assertEqual(user, [{"command": "user-cursor-hook", "user_field": "retain"}])
        owned = [item for item in groups if self.setup.owned_hook_command(item.get("command", ""), "cursor", self.project)]
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0].get("user_field"), "owned-meta")
        self.assertNotEqual(owned[0]["command"], old)

    def test_all_clients_embed_explicit_paths_in_owned_hooks(self) -> None:
        root = str((self.project / "explicit-root").resolve())
        registry = str((self.project / "explicit-registry").resolve())
        with mock.patch.dict(os.environ, {
            coordinator.COORDINATOR_ROOT_ENV: root,
            "VAWS_AGENT_SESSIONS_DIR": registry,
        }):
            for client in ("claude", "cursor", "codex", "grok", "kimi"):
                with self.subTest(client=client):
                    plan = self.setup.build_plan(client, self.project, kimi_config=self.project / "kimi.toml")
                    blob = "\n".join(plan["files"].values())
                    self.assertIn(root, blob)
                    self.assertIn(registry, blob)


class HookAdapterTests(unittest.TestCase):
    def test_explicit_path_flags_work_without_ambient_vaws_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_fake_checkout(Path(tmp) / "checkout")
            _init_git_checkout(root)
            registry = Path(tmp) / "registry with spaces"
            registry.mkdir()
            (root / "hooks" / "vaws_session.py").write_text(
                "import json, os, sys\n"
                "print(json.dumps({"
                "'root': os.environ.get('VAWS_COORDINATOR_ROOT'), "
                "'registry': os.environ.get('VAWS_AGENT_SESSIONS_DIR'), "
                "'argv': sys.argv[1:]}))\n",
                encoding="utf-8",
            )
            env = {key: value for key, value in os.environ.items() if not key.startswith("VAWS_")}
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / ".agents/hooks/vaws_session.py"),
                    "--client", "claude",
                    "--project", tmp,
                    "--coordinator-root", str(root),
                    "--agent-sessions-dir", str(registry),
                ],
                input="{}",
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["root"], str(root))
        self.assertEqual(payload["registry"], str(registry))
        self.assertEqual(payload["argv"], ["--client", "claude", "--project", tmp])


class NoInTreeTaskWriterTests(unittest.TestCase):
    MOVED = (
        ".agents/coordinator",
        ".agents/lib/vaws_agent_session.py",
        ".agents/lib/vaws_task_client.py",
        ".agents/lib/vaws_ready_runtime.py",
        ".agents/lib/vaws_managed_execution.py",
        ".agents/lib/vaws_runtime_profile.py",
    )

    def test_moved_sources_are_not_tracked(self) -> None:
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--", *self.MOVED],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(tracked.stdout.strip(), "", tracked.stdout)

    def test_build_inputs_mirror_remains_for_parity(self) -> None:
        self.assertTrue((ROOT / ".agents/lib/vaws_build_inputs.py").is_file())
        self.assertTrue((ROOT / ".agents/lib/vaws_npu_coordination.py").is_file())
        self.assertTrue((ROOT / ".agents/lib/vaws_run_manifest.py").is_file())


@requires_coordinator
class CoordinatorCheckoutTests(unittest.TestCase):
    def test_pin_matches_the_configured_checkout(self) -> None:
        status = coordinator.checkout_status()
        pin = coordinator.load_dependency()
        mismatched = status["pin_matches"] is False
        if mismatched:
            message = f"checkout {status['commit']} is not the pinned {pin['commit']}"
            if os.environ.get("CI"):
                self.fail(message)
            self.skipTest(message)
        self.assertIn(status["state"], {"ready", "wrong_origin"})
        self.assertEqual(status["commit"], pin["commit"])

    def test_arrival_blobs_match_the_pin(self) -> None:
        pin = coordinator.load_dependency()
        for relative, blob in pin["arrival_blobs"].items():
            result = subprocess.run(
                ["git", "-C", str(CHECKOUT), "rev-parse", f"HEAD:{relative}"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), blob, relative)


if __name__ == "__main__":
    unittest.main()
