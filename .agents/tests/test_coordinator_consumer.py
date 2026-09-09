"""Scaffold-side contract with the installed vaws-coordinator package.

Launcher, client-setup preservation and the build-input byte match run
against the package. Official MCP SDK coverage lives in
``test_coordinator_official_stdio.py``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

_MCP_SERVER_TABLE = re.compile(r"^\[mcp_servers\.([^.\]]+)\]\s*$", re.M)
_PACKAGED_REMOTE_ARGS = ["-m", "remote_dev.mcp.server"]
_STALE_RELATIVE_ARGS = [".agents/scripts/remote_dev.py", "server"]

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_coordinator_launch as coordinator  # noqa: E402

_GONE_COORDINATOR_ROOT = "VAWS_" + "COORDINATOR_ROOT"

requires_package = unittest.skipUnless(
    importlib.util.find_spec("vaws_coordinator") is not None,
    "vaws-coordinator is not installed; run `uv sync`",
)


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def isolated_python():
    """Return a 3.11+ interpreter that does not see the workspace ``.venv``."""
    base = Path(sys.base_prefix) / "bin" / "python3"
    if base.is_file() and base.resolve() != Path(sys.executable).resolve():
        return str(base)
    for candidate in ("/opt/homebrew/bin/python3", "/usr/bin/python3"):
        path = Path(candidate)
        if not path.is_file():
            continue
        proc = subprocess.run(
            [str(path), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"],
            check=False,
        )
        if proc.returncode == 0:
            return str(path)
    raise unittest.SkipTest("no Python 3.11+ interpreter outside .venv")


def isolated_env():
    drop = {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in drop and not key.startswith("UV_")
    }
    env["VAWS_SKIP_VENV_REEXEC"] = "1"
    return env


class EnvironmentTests(unittest.TestCase):
    def test_environment_fills_the_single_registry(self) -> None:
        env = coordinator.coordinator_environment({})
        self.assertTrue(env["VAWS_AGENT_SESSIONS_DIR"].endswith("agent-sessions"))
        self.assertNotIn("VAWS_HOST_QUEUE_MODULE", env)
        self.assertNotIn("VAWS_COORDINATOR_STATE_DIR", env)
        self.assertNotIn(_GONE_COORDINATOR_ROOT, env)

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


class BuildInputOwnershipTests(unittest.TestCase):
    def test_scaffold_does_not_keep_a_copy(self) -> None:
        self.assertFalse((ROOT / ".agents/lib/vaws_build_inputs.py").is_file())
        try:
            import vaws_coordinator.build_inputs as packaged
        except ImportError:
            self.skipTest("vaws-coordinator is not installed")
        self.assertTrue(Path(packaged.__file__).is_file())


class LauncherTests(unittest.TestCase):
    def test_status_reports_the_installed_package(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws.py"), "status"],
            capture_output=True, text=True, check=False,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["name"], "vaws-coordinator")
        self.assertIn(payload["state"], {"missing", "off_spec", "ready"})
        self.assertEqual(payload["remedy"], "uv sync")
        self.assertIsNone(payload["manager_state_dir_default"])

    def test_status_without_package_reports_missing(self) -> None:
        proc = subprocess.run(
            [isolated_python(), str(SCRIPTS / "vaws.py"), "status"],
            capture_output=True, text=True, env=isolated_env(), check=False,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"], "missing")
        self.assertEqual(payload["remedy"], "uv sync")

    def test_task_server_and_task_ops_without_package_fail_closed(self) -> None:
        env = isolated_env()
        python = isolated_python()
        proc = subprocess.run(
            [python, str(SCRIPTS / "vaws.py"), "task-server"],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("uv sync", proc.stderr)
        for operation in ("attach", "session", "run", "execution", "finish"):
            argv = [python, str(SCRIPTS / "vaws.py"), operation]
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
            self.assertIn("uv sync", payload["summary"] + json.dumps(payload) + child.stderr)

    def test_hook_without_package_does_not_write_a_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = isolated_env()
            proc = subprocess.run(
                [isolated_python(), str(ROOT / ".agents/hooks/vaws_session.py"), "--client", "codex"],
                input='{"hook_event_name":"SessionStart","session_id":"n1"}',
                capture_output=True, text=True, env=env, check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("uv sync", proc.stderr)
        self.assertFalse(list(Path(tmp).rglob("sessions.sqlite3")))

    def test_env_json_lists_owned_keys(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws.py"), "env", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("VAWS_AGENT_SESSIONS_DIR", payload)
        self.assertNotIn("VAWS_HOST_QUEUE_MODULE", payload)
        self.assertNotIn(_GONE_COORDINATOR_ROOT, payload)
        self.assertTrue(all(key.startswith("VAWS_") for key in payload))

    def test_help_matrix(self) -> None:
        for args in (["--help"], ["status", "--help"]):
            with self.subTest(args=args):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / "vaws.py"), *args],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws.py"), "bootstrap", "--help"],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(proc.returncode, 0)


class ClientSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.setup = load_script("vaws_client_setup")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve() / "project"
        self.project.mkdir()
        patcher = mock.patch.dict(os.environ, {_GONE_COORDINATOR_ROOT: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_json_emits_both_launchers(self) -> None:
        files = self.setup.configuration("claude", self.project)
        servers = json.loads(files[self.project / ".mcp.json"])["mcpServers"]
        self.assertEqual(set(servers), {"remote-dev", "vaws-task", "vaws-knowledge"})
        self.assertEqual(servers["remote-dev"]["args"], ["-m", "remote_dev.mcp.server"])
        self.assertEqual(servers["vaws-task"]["args"], ["-m", "vaws_coordinator", "task-server"])
        self.assertEqual(servers["vaws-task"]["type"], "stdio")
        self.assertIn("VAWS_AGENT_SESSIONS_DIR", servers["vaws-task"]["env"])
        self.assertNotIn("VAWS_HOST_QUEUE_MODULE", servers["vaws-task"]["env"])
        self.assertNotIn(_GONE_COORDINATOR_ROOT, servers["vaws-task"]["env"])
        hook = json.loads(files[self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("--agent-sessions-dir", hook)
        self.assertNotIn("--coordinator-root", hook)

    def test_task_only_skips_remote_dev(self) -> None:
        servers = json.loads(
            self.setup.configuration("claude", self.project, task_only=True)[self.project / ".mcp.json"]
        )["mcpServers"]
        self.assertEqual(set(servers), {"vaws-task"})
        grok = tomllib.loads(
            self.setup.configuration("grok", self.project, task_only=True)[self.project / ".grok/config.toml"]
        )
        self.assertEqual(set(grok["mcp_servers"]), {"vaws_task"})

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

    def _apply_client(self, client, project):
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "vaws_client_setup.py"),
                "--client", client, "--project", str(project), "--apply",
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _mcp_tables(self, text):
        return _MCP_SERVER_TABLE.findall(text)

    def _remote_tables(self, text):
        return [name for name in self._mcp_tables(text) if name.replace("_", "-") == "remote-dev"]

    def _write_stale_toml(self, project, client, table):
        config = project / f".{client}" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f"[mcp_servers.{table}]\n"
            'command = "python3"\n'
            f"args = {json.dumps(_STALE_RELATIVE_ARGS)}\n"
            "[mcp_servers.unrelated-external]\n"
            'command = "/usr/bin/true"\n'
            'args = ["ok"]\n'
        )
        return config

    def _assert_stale_remote_rewritten(self, payload, config, stale_table):
        self.assertTrue(
            any(
                note.get("action") == "rewritten-stale" and note.get("server") == "remote-dev"
                for note in payload["rewritten_servers"]
            ),
            payload,
        )
        text = config.read_text()
        self.assertNotIn(f"[mcp_servers.{stale_table}]", text)
        self.assertEqual(self._remote_tables(text), ["remote_dev"])
        self.assertIn("unrelated-external", self._mcp_tables(text))
        data = tomllib.loads(text)
        self.assertNotIn("remote-dev", data["mcp_servers"])
        self.assertEqual(data["mcp_servers"]["remote_dev"]["args"], _PACKAGED_REMOTE_ARGS)
        self.assertEqual(data["mcp_servers"]["unrelated-external"]["command"], "/usr/bin/true")

    def test_json_rewrites_stale_checkout_paths(self) -> None:
        gone = self.project / ".agents" / "scripts" / "remote_dev.py"
        path = self.project / ".mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "remote-dev": {
                    "command": "python3",
                    "args": [str(gone), "server"],
                    "type": "stdio",
                }
            }
        }))
        plan = self.setup.build_plan("claude", self.project)
        data = json.loads(plan["files"][path])
        entry = data["mcpServers"]["remote-dev"]
        self.assertEqual(entry["args"], _PACKAGED_REMOTE_ARGS)
        self.assertNotIn("remote_dev", data["mcpServers"])
        self.assertTrue(
            any(note.get("action") == "rewritten-stale" for note in plan["notes"])
        )

    def test_json_apply_drops_underscore_stale_key(self) -> None:
        path = self.project / ".mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "remote_dev": {
                    "command": "python3",
                    "args": list(_STALE_RELATIVE_ARGS),
                    "type": "stdio",
                },
                "unrelated-external": {"command": "/usr/bin/true", "args": ["ok"]},
            }
        }))
        payload = self._apply_client("claude", self.project)
        self.assertTrue(
            any(note.get("action") == "rewritten-stale" for note in payload["rewritten_servers"]),
            payload,
        )
        data = json.loads(path.read_text())
        self.assertNotIn("remote_dev", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["remote-dev"]["args"], _PACKAGED_REMOTE_ARGS)
        self.assertEqual(data["mcpServers"]["unrelated-external"]["command"], "/usr/bin/true")

    def test_toml_rewrites_stale_checkout_paths(self) -> None:
        gone = self.project / ".agents" / "scripts" / "remote_dev.py"
        config = self.project / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text(
            "[mcp_servers.remote_dev]\n"
            'command = "python3"\n'
            f'args = ["{gone}", "server"]\n'
        )
        plan = self.setup.build_plan("codex", self.project)
        text = plan["files"][config]
        data = tomllib.loads(text)
        self.assertEqual(data["mcp_servers"]["remote_dev"]["args"], _PACKAGED_REMOTE_ARGS)
        self.assertNotIn("remote-dev", data["mcp_servers"])
        self.assertEqual(self._remote_tables(text), ["remote_dev"])
        self.assertTrue(
            any(note.get("action") == "rewritten-stale" for note in plan["notes"])
        )

    def test_toml_apply_drops_hyphen_stale_table(self) -> None:
        for client in ("codex", "grok"):
            with self.subTest(client=client):
                project = self.project / client
                project.mkdir()
                config = self._write_stale_toml(project, client, "remote-dev")
                payload = self._apply_client(client, project)
                self._assert_stale_remote_rewritten(payload, config, "remote-dev")

    def test_toml_apply_drops_underscore_stale_table(self) -> None:
        for client in ("codex", "grok"):
            with self.subTest(client=client):
                project = self.project / f"{client}-underscore"
                project.mkdir()
                config = self._write_stale_toml(project, client, "remote_dev")
                payload = self._apply_client(client, project)
                self._assert_stale_remote_rewritten(payload, config, "remote-dev")
                text = config.read_text()
                self.assertEqual(text.count("[mcp_servers.remote_dev]"), 1)

    def test_toml_apply_repairs_hyphen_and_underscore_duplicate(self) -> None:
        config = self.project / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text(
            "[mcp_servers.remote-dev]\n"
            'command = "python3"\n'
            f"args = {json.dumps(_STALE_RELATIVE_ARGS)}\n"
            "[mcp_servers.remote_dev]\n"
            'command = "python3"\n'
            f"args = {json.dumps(_PACKAGED_REMOTE_ARGS)}\n"
        )
        payload = self._apply_client("codex", self.project)
        self.assertTrue(
            any(note.get("action") == "rewritten-stale" for note in payload["rewritten_servers"]),
            payload,
        )
        text = config.read_text()
        self.assertNotIn("[mcp_servers.remote-dev]", text)
        self.assertEqual(self._remote_tables(text), ["remote_dev"])
        data = tomllib.loads(text)
        self.assertEqual(data["mcp_servers"]["remote_dev"]["args"], _PACKAGED_REMOTE_ARGS)

    def test_toml_apply_preserves_external_and_living_checkout_paths(self) -> None:
        hyphen = self.project / "hyphen-external"
        hyphen.mkdir()
        hyphen_config = hyphen / ".codex" / "config.toml"
        hyphen_config.parent.mkdir(parents=True)
        hyphen_config.write_text(
            "[mcp_servers.remote-dev]\n"
            'command = "python3"\n'
            'args = ["/usr/bin/true"]\n'
            "[mcp_servers.unrelated-external]\n"
            'command = "/usr/bin/true"\n'
            'args = ["ok"]\n'
        )
        payload = self._apply_client("codex", hyphen)
        self.assertTrue(
            any(
                note.get("action") == "preserved" and note.get("server") == "remote-dev"
                for note in payload["preserved_servers"]
            ),
            payload,
        )
        self.assertFalse(
            any(note.get("server") == "remote-dev" for note in payload["rewritten_servers"]),
            payload,
        )
        hyphen_text = hyphen_config.read_text()
        self.assertIn("[mcp_servers.remote-dev]", hyphen_text)
        self.assertNotIn("[mcp_servers.remote_dev]", hyphen_text)
        self.assertIn("unrelated-external", self._mcp_tables(hyphen_text))
        self.assertEqual(
            tomllib.loads(hyphen_text)["mcp_servers"]["remote-dev"]["args"],
            ["/usr/bin/true"],
        )

        living = self.project / "living-checkout"
        living.mkdir()
        kept = living / ".agents" / "scripts" / "remote_dev.py"
        kept.parent.mkdir(parents=True)
        kept.write_text("# still here\n")
        living_config = living / ".codex" / "config.toml"
        living_config.parent.mkdir(parents=True)
        living_config.write_text(
            "[mcp_servers.remote_dev]\n"
            'command = "python3"\n'
            f'args = ["{kept}", "server"]\n'
        )
        payload = self._apply_client("codex", living)
        self.assertTrue(
            any(
                note.get("action") == "preserved" and note.get("server") == "remote-dev"
                for note in payload["preserved_servers"]
            ),
            payload,
        )
        self.assertFalse(
            any(note.get("server") == "remote-dev" for note in payload["rewritten_servers"]),
            payload,
        )
        living_text = living_config.read_text()
        self.assertIn("[mcp_servers.remote_dev]", living_text)
        self.assertNotIn("[mcp_servers.remote-dev]", living_text)
        self.assertEqual(
            tomllib.loads(living_text)["mcp_servers"]["remote_dev"]["args"],
            [str(kept), "server"],
        )

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
        self.assertEqual(data["mcp_servers"]["vaws_task"]["args"], ["-m", "vaws_coordinator", "task-server"])
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

    def test_generated_provider_and_hook_keep_explicit_registry(self) -> None:
        registry = (self.project / "reg dir").resolve()
        with mock.patch.dict(os.environ, {
            "VAWS_AGENT_SESSIONS_DIR": str(registry),
        }):
            plan = self.setup.build_plan("claude", self.project, task_only=True)
        server = json.loads(plan["files"][self.project / ".mcp.json"])["mcpServers"]["vaws-task"]
        self.assertNotIn(_GONE_COORDINATOR_ROOT, server["env"])
        self.assertEqual(server["env"]["VAWS_AGENT_SESSIONS_DIR"], str(registry))
        hook = json.loads(plan["files"][self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        argv = shlex.split(hook)
        self.assertNotIn("--coordinator-root", argv)
        self.assertEqual(argv[argv.index("--agent-sessions-dir") + 1], str(registry))

    def test_existing_user_env_wins_over_setup_registry(self) -> None:
        path = self.project / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": {"vaws-task": {
            "command": "user-command",
            "args": ["user-argument"],
            "env": {"VAWS_AGENT_SESSIONS_DIR": "/user/managed/registry"},
            "user_field": 1,
        }}}))
        with mock.patch.dict(os.environ, {
            "VAWS_AGENT_SESSIONS_DIR": str(self.project / "setup-registry"),
        }):
            plan = self.setup.build_plan("claude", self.project, task_only=True)
        server = json.loads(plan["files"][path])["mcpServers"]["vaws-task"]
        self.assertEqual(server["command"], "user-command")
        self.assertEqual(server["env"]["VAWS_AGENT_SESSIONS_DIR"], "/user/managed/registry")
        self.assertEqual(server["user_field"], 1)
        hook = json.loads(plan["files"][self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        argv = shlex.split(hook)
        self.assertNotIn("--coordinator-root", argv)
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
        with mock.patch.dict(os.environ, {}):
            first = self.setup.build_plan("claude", self.project)
            groups = json.loads(first["files"][settings])["hooks"]["SessionStart"]
            commands = [entry.get("command", "") for group in groups for entry in group.get("hooks", [group])]
            self.assertEqual(commands.count("my-hook"), 1)
            owned = [item for item in commands if "vaws_session.py" in item]
            self.assertEqual(len(owned), 1)
            self.assertNotIn("--coordinator-root", owned[0])
            self.assertIn("--agent-sessions-dir", owned[0])
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
        with mock.patch.dict(os.environ, {}):
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
            self.assertNotIn("--coordinator-root", owned[0]["command"])
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

    def test_all_clients_embed_explicit_registry_in_owned_hooks(self) -> None:
        registry = str((self.project / "explicit-registry").resolve())
        with mock.patch.dict(os.environ, {
            "VAWS_AGENT_SESSIONS_DIR": registry,
        }):
            for client in ("claude", "cursor", "codex", "grok", "kimi"):
                with self.subTest(client=client):
                    plan = self.setup.build_plan(client, self.project, kimi_config=self.project / "kimi.toml")
                    blob = "\n".join(plan["files"].values())
                    self.assertNotIn(_GONE_COORDINATOR_ROOT, blob)
                    self.assertIn(registry, blob)


class HookAdapterTests(unittest.TestCase):
    def test_explicit_registry_flag_is_forwarded_and_root_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry with spaces"
            registry.mkdir()
            env = {key: value for key, value in os.environ.items()}
            # Package is installed in the test interpreter; --coordinator-root must be accepted and ignored.
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / ".agents/hooks/vaws_session.py"),
                    "--client", "claude",
                    "--project", tmp,
                    "--coordinator-root", str(Path(tmp) / "ignored-root"),
                    "--agent-sessions-dir", str(registry),
                ],
                input='{"hook_event_name":"SessionStart","session_id":"n1","cwd":"%s"}' % tmp,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        self.assertIn(proc.returncode, {0, 1}, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn(_GONE_COORDINATOR_ROOT, proc.stderr)


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

    def test_build_inputs_live_in_the_coordinator_package(self) -> None:
        self.assertFalse((ROOT / ".agents/lib/vaws_build_inputs.py").is_file())
        self.assertTrue((ROOT / ".agents/lib/vaws_host_queue_module.py").is_file())
        self.assertFalse((ROOT / ".agents/lib/vaws_run_manifest.py").is_file())
        import vaws_coordinator.build_inputs  # noqa: F401
        import vaws_coordinator.run_manifest  # noqa: F401




if __name__ == "__main__":
    unittest.main()
