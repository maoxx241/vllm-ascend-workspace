"""Scaffold-side contract with the external remote-dev substrate.

Everything here runs without a remote-dev checkout except the tests marked
``requires_substrate``, which exercise the real resolver registration through
``core.endpoint`` when ``VAWS_REMOTE_DEV_ROOT`` points at one (CI provides it
only when a read token for the private repository is configured).
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_remote_dev as remote_dev  # noqa: E402
import vaws_remote_dev_plugin as plugin  # noqa: E402
import vaws_session_id  # noqa: E402
from vaws_remote_toolbox import RemoteToolboxError  # noqa: E402

SUBSTRATE = remote_dev.remote_dev_root(required=False)
requires_substrate = unittest.skipUnless(SUBSTRATE, "no remote-dev checkout (set VAWS_REMOTE_DEV_ROOT)")


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fake_target(*, session_id=None, alias="host-a", port=46000, runtime_root="/vllm-workspace"):
    endpoint = SimpleNamespace(host="203.0.113.10", port=port, user="root")
    return SimpleNamespace(
        container_endpoint=endpoint,
        session_id=session_id,
        alias=alias,
        runtime_root=runtime_root,
        to_dict=lambda: {"mode": "session" if session_id else "legacy", "alias": alias, "session_id": session_id},
    )


class FindSessionBindingTests(unittest.TestCase):
    """The cwd-upward auto-bind walk stops at the repository root (D2).

    Moved from the substrate's ``tests/test_endpoint.py``: the walk is scaffold
    behaviour (``vaws_session_id``) that the resolver plugin now relies on.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.repo = base / "repo"
        self.deep = self.repo / "a" / "b"
        self.deep.mkdir(parents=True)
        self.elsewhere = base / "elsewhere"
        self.elsewhere.mkdir()
        self._write_binding(base, "sess-stray-above-repo")

    def _write_binding(self, directory: Path, session_id: str) -> None:
        state = directory / ".vaws-local"
        state.mkdir(parents=True, exist_ok=True)
        (state / "current-session.json").write_text(
            json.dumps({"schema_version": 1, "session_id": session_id, "source": "test"}), encoding="utf-8"
        )

    def test_walk_finds_binding_at_or_below_repo_root(self) -> None:
        self._write_binding(self.repo, "sess-in-repo")
        with mock.patch.object(vaws_session_id, "_binding_walk_stop", return_value=self.repo):
            found = vaws_session_id.find_session_binding(self.deep)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], self.repo)
        self.assertEqual(found[1]["session_id"], "sess-in-repo")

    def test_walk_ignores_stray_binding_above_repo_root(self) -> None:
        with mock.patch.object(vaws_session_id, "_binding_walk_stop", return_value=self.repo):
            self.assertIsNone(vaws_session_id.find_session_binding(self.deep))

    def test_walk_outside_repo_keeps_legacy_unbounded_behavior(self) -> None:
        with mock.patch.object(vaws_session_id, "_binding_walk_stop", return_value=self.repo):
            found = vaws_session_id.find_session_binding(self.elsewhere)
        self.assertIsNotNone(found)
        self.assertEqual(found[1]["session_id"], "sess-stray-above-repo")


class ResolverPluginMappingTests(unittest.TestCase):
    """The plugin reproduces the substrate's former `_endpoint_from_managed`."""

    def test_machine_maps_to_container_endpoint_with_ascend_runtime_env(self) -> None:
        with mock.patch.object(plugin, "resolve_remote_target", return_value=fake_target()) as resolver:
            payload = plugin.resolve_vaws({"machine": "host-a", "root": "/vllm-workspace"})
        resolver.assert_called_once_with(repo_root=plugin.REPO_ROOT, machine="host-a", session_id=None, session_file=None)
        self.assertEqual(payload["host"], "203.0.113.10")
        self.assertEqual(payload["port"], 46000)
        self.assertEqual(payload["user"], "root")
        self.assertEqual(payload["cwd"], "/vllm-workspace")
        self.assertEqual(payload["kind"], "managed-machine")
        self.assertEqual(payload["alias"], "host-a")
        self.assertEqual(payload["runtime_env_file"], remote_dev.ASCEND_RUNTIME_ENV_FILE)
        self.assertEqual(payload["source"], {"vaws_target": fake_target().to_dict()})
        # Caller overrides (root/cwd/user/...) are merged by the substrate, so
        # the plugin must not pre-empt them.
        self.assertNotIn("root", payload)

    def test_session_selector_marks_managed_session(self) -> None:
        target = fake_target(session_id="sess-1", alias="host-b", port=46008, runtime_root="/vllm-workspace/s1")
        with mock.patch.object(plugin, "resolve_remote_target", return_value=target):
            payload = plugin.resolve_vaws({"session_id": "sess-1"})
        self.assertEqual(payload["kind"], "managed-session")
        self.assertEqual(payload["alias"], "sess-1")
        self.assertEqual(payload["cwd"], "/vllm-workspace/s1")

    def test_worktree_auto_bind_uses_session_id_as_alias(self) -> None:
        target = fake_target(session_id="sess-bound", alias="host-b")
        with mock.patch.object(plugin, "resolve_remote_target", return_value=target):
            payload = plugin.resolve_vaws({})
        self.assertEqual(payload["kind"], "managed-session")
        self.assertEqual(payload["alias"], "sess-bound")

    def test_no_selector_and_no_binding_declines(self) -> None:
        with mock.patch.object(plugin, "resolve_remote_target", side_effect=RemoteToolboxError("no binding")):
            self.assertIsNone(plugin.resolve_vaws({}))
            self.assertIsNone(plugin.resolve_vaws({"root": "/vllm-workspace"}))

    def test_selector_that_cannot_resolve_raises(self) -> None:
        with mock.patch.object(plugin, "resolve_remote_target", side_effect=RemoteToolboxError("not found")):
            with self.assertRaises(Exception) as ctx:
                plugin.resolve_vaws({"machine": "ghost"})
        self.assertIn("ghost", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    def test_setup_is_marked_as_substrate_setup_hook(self) -> None:
        self.assertTrue(getattr(plugin.setup, "remote_dev_resolver_setup", False))
        self.assertEqual(plugin.FIELDS, ("machine", "session_id", "session_file"))

    def test_plugin_imports_nothing_from_the_substrate_at_import_time(self) -> None:
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('p', {str(plugin.__file__)!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "print(any(name == 'core' or name.startswith('core.') for name in sys.modules))\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")


class LocatorTests(unittest.TestCase):
    def test_env_root_must_look_like_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(remote_dev.RemoteDevUnavailable) as ctx:
                remote_dev.remote_dev_root(env={remote_dev.REMOTE_DEV_ROOT_ENV: tmp})
            self.assertIn("not a remote-dev checkout", str(ctx.exception))
            self.assertIsNone(remote_dev.remote_dev_root(required=False, env={remote_dev.REMOTE_DEV_ROOT_ENV: tmp}))

    def test_fake_checkout_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in remote_dev.REQUIRED_FILES:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text("", encoding="utf-8")
            self.assertEqual(remote_dev.remote_dev_root(env={remote_dev.REMOTE_DEV_ROOT_ENV: tmp}), root.resolve())
            status = remote_dev.checkout_status({remote_dev.REMOTE_DEV_ROOT_ENV: tmp})
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["root_source"], "env")

    def test_substrate_environment_fills_defaults_and_absolutises_paths(self) -> None:
        env = remote_dev.substrate_environment({"REMOTE_DEV_RESOLVERS": ".agents/lib/vaws_remote_dev_plugin.py:setup",
                                                "REMOTE_DEV_STATE_DIR": ".vaws-local/remote-dev-state"})
        self.assertEqual(env["REMOTE_DEV_RUNTIME_ENV_FILE"], "/etc/profile.d/vaws-ascend-env.sh")
        self.assertEqual(env["REMOTE_DEV_SSH_MUX_DIR"], "~/.ssh/vaws-mux")
        self.assertEqual(env["REMOTE_DEV_RESOLVERS"], f"{(ROOT / '.agents/lib/vaws_remote_dev_plugin.py').resolve()}:setup")
        self.assertEqual(env["REMOTE_DEV_STATE_DIR"], str(ROOT / ".vaws-local/remote-dev-state"))
        # Permission defaults are left to the client configuration.
        self.assertNotIn("REMOTE_DEV_DEFAULT_ROOT", env)

    def test_substrate_environment_keeps_caller_values(self) -> None:
        env = remote_dev.substrate_environment({"REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/other.sh",
                                                "REMOTE_DEV_RESOLVERS": "/abs/plugin.py:setup,pkg.mod:resolver"})
        self.assertEqual(env["REMOTE_DEV_RUNTIME_ENV_FILE"], "/etc/profile.d/other.sh")
        self.assertEqual(env["REMOTE_DEV_RESOLVERS"], "/abs/plugin.py:setup,pkg.mod:resolver")

    def test_dependency_pin_names_the_private_repository_and_a_commit(self) -> None:
        pin = remote_dev.load_dependency()
        self.assertEqual(pin["repository"], "vllm-ascend-workspace/remote-dev")
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(pin["visibility"], "private")


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.launcher = load_script("remote_dev")

    def test_legacy_selector_flags_become_selectors(self) -> None:
        translate = self.launcher.translate_legacy_selectors
        self.assertEqual(translate(["--machine", "host-a", "--command", "nproc"]),
                         ["--selector", "machine=host-a", "--command", "nproc"])
        self.assertEqual(translate(["--session-id=s1", "--root", "/x"]), ["--selector", "session_id=s1", "--root", "/x"])
        self.assertEqual(translate(["--session-file", "/p/session.json"]), ["--selector", "session_file=/p/session.json"])
        self.assertEqual(translate(["--selector", "machine=x"]), ["--selector", "machine=x"])
        with self.assertRaises(ValueError):
            translate(["--machine"])

    def test_status_without_checkout_reports_missing_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, remote_dev.REMOTE_DEV_ROOT_ENV: str(Path(tmp) / "absent")}
            proc = subprocess.run([sys.executable, str(SCRIPTS / "remote_dev.py"), "status"], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"], "missing")
        self.assertEqual(payload["root_source"], "env")
        self.assertTrue(payload["resolver"].endswith("vaws_remote_dev_plugin.py:setup"))

    def test_hook_without_checkout_allows_and_consumes_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, remote_dev.REMOTE_DEV_ROOT_ENV: str(Path(tmp) / "absent")}
            proc = subprocess.run([sys.executable, str(SCRIPTS / "remote_dev.py"), "hook", "claude"], input='{"tool_name": "Bash"}',
                                  capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("skipped", proc.stderr)

    def test_server_and_tool_without_checkout_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, remote_dev.REMOTE_DEV_ROOT_ENV: str(Path(tmp) / "absent")}
            for argv in (["server"], ["tool", "remote_bash", "--command", "true"]):
                proc = subprocess.run([sys.executable, str(SCRIPTS / "remote_dev.py"), *argv], capture_output=True, text=True, env=env, check=False)
                self.assertEqual(proc.returncode, 2, argv)
                self.assertIn("bootstrap", proc.stderr)

    def test_env_json_lists_substrate_keys(self) -> None:
        proc = subprocess.run([sys.executable, str(SCRIPTS / "remote_dev.py"), "env", "--json"], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("REMOTE_DEV_RESOLVERS", payload)
        self.assertIn("REMOTE_DEV_RUNTIME_ENV_FILE", payload)
        self.assertTrue(all(key.startswith("REMOTE_DEV_") for key in payload))


class ClientConfigurationTests(unittest.TestCase):
    LAUNCHER_ARGS = [".agents/scripts/remote_dev.py", "server"]
    REQUIRED_ENV = ("REMOTE_DEV_RUNTIME_ENV_FILE", "REMOTE_DEV_RESOLVERS", "REMOTE_DEV_STATE_DIR")

    def test_tracked_json_clients_use_the_launcher_and_inject_the_environment(self) -> None:
        for relative in (".mcp.json", ".cursor/mcp.json"):
            with self.subTest(file=relative):
                servers = json.loads((ROOT / relative).read_text(encoding="utf-8"))["mcpServers"]
                entry = servers["remote-dev"]
                self.assertEqual(entry["args"], self.LAUNCHER_ARGS)
                self.assertEqual(servers["vaws-task"]["args"], [".agents/scripts/vaws.py", "task-server"])
                for key in self.REQUIRED_ENV:
                    self.assertIn(key, entry["env"])
                self.assertEqual(entry["env"]["REMOTE_DEV_RUNTIME_ENV_FILE"], remote_dev.ASCEND_RUNTIME_ENV_FILE)
                self.assertEqual(entry["env"]["REMOTE_DEV_RESOLVERS"], ".agents/lib/vaws_remote_dev_plugin.py:setup")

    def test_tracked_toml_examples_use_the_launcher_and_inject_the_environment(self) -> None:
        for relative, server in ((".codex/config.example.toml", "remote_dev"), (".grok/config.example.toml", "remote-dev")):
            with self.subTest(file=relative):
                data = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
                entry = data["mcp_servers"][server]
                self.assertTrue(entry["args"][0].endswith("/.agents/scripts/remote_dev.py"), entry["args"])
                self.assertEqual(entry["args"][1], "server")
                task = data["mcp_servers"].get("vaws_task") or data["mcp_servers"]["vaws-task"]
                self.assertEqual(task["args"][1], "task-server")
                for key in self.REQUIRED_ENV:
                    self.assertIn(key, entry["env"])
                self.assertTrue(entry["env"]["REMOTE_DEV_RESOLVERS"].endswith("vaws_remote_dev_plugin.py:setup"))

    def test_claude_and_codex_hooks_go_through_the_launcher(self) -> None:
        settings = json.loads((ROOT / ".claude/settings.example.json").read_text(encoding="utf-8"))
        commands = [hook["command"] for group in settings["hooks"]["PreToolUse"] for hook in group["hooks"]]
        self.assertTrue(commands)
        self.assertTrue(all(command == "python3 .agents/scripts/remote_dev.py hook claude" for command in commands), commands)
        codex = (ROOT / ".codex/config.example.toml").read_text(encoding="utf-8")
        self.assertIn(".agents/scripts/remote_dev.py\" hook codex", codex)

    def test_client_setup_emits_launcher_entry_with_environment(self) -> None:
        setup = load_script("vaws_client_setup")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            files = setup.configuration("claude", project)
            servers = json.loads(files[project / ".mcp.json"])["mcpServers"]
            mcp = servers["remote-dev"]
            self.assertEqual(set(servers), {"remote-dev", "vaws-task"})
            self.assertEqual(mcp["args"], [str(ROOT / ".agents/scripts/remote_dev.py"), "server"])
            self.assertEqual(servers["vaws-task"]["args"], [str(ROOT / ".agents/scripts/vaws.py"), "task-server"])
            for key in self.REQUIRED_ENV:
                self.assertIn(key, mcp["env"])
            codex = tomllib.loads(setup.configuration("codex", project)[project / ".codex/config.toml"])
            self.assertEqual(codex["mcp_servers"]["remote_dev"]["args"][1], "server")
            self.assertEqual(codex["mcp_servers"]["vaws_task"]["args"][1], "task-server")
            self.assertIn("REMOTE_DEV_RESOLVERS", codex["mcp_servers"]["remote_dev"]["env"])
        self.assertFalse(str(setup.BACKUP_DIR).startswith(str(ROOT / ".remote-dev")))
        self.assertTrue(str(setup.BACKUP_DIR).startswith(str(ROOT / ".vaws-local")))

    def test_client_setup_keeps_user_environment_values(self) -> None:
        setup = load_script("vaws_client_setup")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote-dev": {"env": {"REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/custom.sh"}}}}))
            mcp = json.loads(setup.configuration("claude", project)[project / ".mcp.json"])["mcpServers"]["remote-dev"]
            self.assertEqual(mcp["env"]["REMOTE_DEV_RUNTIME_ENV_FILE"], "/etc/profile.d/custom.sh")


class NoInTreeSubstrateTests(unittest.TestCase):
    """The old vendored copy is gone and nothing executable still reads it."""

    def test_in_tree_copy_is_deleted(self) -> None:
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", ".remote-dev"], capture_output=True, text=True, check=False)
        self.assertEqual(tracked.stdout.strip(), "", "the in-tree .remote-dev copy must not be tracked")

    @staticmethod
    def _python_string_constants(source: str) -> list[str]:
        """String constants with module/class/function docstrings excluded.

        Comments never reach the AST and docstrings may explain history; only
        a string a program can act on counts as a reference.
        """
        tree = ast.parse(source)
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body:
                first = body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
        return [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]

    def test_no_tracked_code_or_config_references_the_old_path(self) -> None:
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=False).stdout.split("\0")
        exempt = {
            ".agents/tests/test_remote_dev_consumer.py",
            ".agents/tests/test_repo_boundary_check.py",  # names the old path as a guard fixture
            # The boundary policy and its baseline explain *why* the coupling
            # existed; that prose is history, not a path anything reads.
            ".agents/policy/repo-boundaries.json",
            ".agents/policy/repo-boundaries-baseline.json",
        }
        offenders = []
        for relative in tracked:
            path = ROOT / relative
            if not relative or relative in exempt or not path.is_file():
                continue
            suffix = path.suffix
            if suffix == ".py":
                haystack = "\n".join(self._python_string_constants(path.read_text(encoding="utf-8", errors="replace")))
            elif suffix in {".json", ".toml", ".yml", ".yaml"}:
                haystack = path.read_text(encoding="utf-8", errors="replace")
            else:
                continue
            if ".remote-dev/" in haystack:
                offenders.append(relative)
        self.assertEqual(offenders, [])

    def test_vaws_cli_reports_moved_task_tools_without_traceback(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "VAWS_COORDINATOR_ROOT"}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "vaws.py"), "session", "--json", "{}"], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout)["result"]
        self.assertEqual((payload["tool"], payload["outcome"], payload["status"]), ("vaws.session", "blocked", "unavailable"))
        self.assertIn("vaws-coordinator", payload["summary"] + json.dumps(payload))

    def test_vaws_cli_help_matrix(self) -> None:
        for args in (["--help"], ["attach", "--help"], ["session", "--help"], ["run", "--help"], ["execution", "--help"], ["finish", "--help"]):
            with self.subTest(args=args):
                proc = subprocess.run([sys.executable, str(SCRIPTS / "vaws.py"), *args], capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)


class ClaudeSkillShimTests(unittest.TestCase):
    """Moved with `sync_claude_skills.py` from the substrate's test_cli_help.py."""

    def test_claude_skill_shim_check_passes(self) -> None:
        proc = subprocess.run([sys.executable, str(SCRIPTS / "sync_claude_skills.py"), "--check"], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_claude_skill_shim_check_reports_unexpected_files(self) -> None:
        module = load_script("sync_claude_skills")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "agents-skills" / "demo-skill"
            source_dir.mkdir(parents=True)
            (source_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo skill.\n---\n\n# Demo\n", encoding="utf-8")
            shim_dir = tmp_path / "claude-skills" / "demo-skill"
            shim_dir.mkdir(parents=True)
            module.AGENTS_SKILLS = tmp_path / "agents-skills"
            module.CLAUDE_SKILLS = tmp_path / "claude-skills"
            (shim_dir / "SKILL.md").write_text(module.expected_skill_body(source_dir), encoding="utf-8")
            self.assertEqual(module.check_shims(), [])
            (shim_dir / "legacy-notes.md").write_text("stale\n", encoding="utf-8")
            self.assertEqual(module.check_shims(), ["unexpected file in Claude skill shim demo-skill: legacy-notes.md"])

    def test_claude_skills_are_lightweight_shims(self) -> None:
        for source in sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md")):
            target = ROOT / ".claude" / "skills" / source.parent.name / "SKILL.md"
            with self.subTest(skill=source.parent.name):
                self.assertTrue(target.exists())
                body = target.read_text(encoding="utf-8")
                self.assertIn(f"`.agents/skills/{source.parent.name}/SKILL.md`", body)
                self.assertLessEqual(len(body.splitlines()), 60)
                self.assertNotEqual(body, source.read_text(encoding="utf-8"))
                self.assertNotIn("`.remote-dev`", body)


@requires_substrate
class SubstrateIntegrationTests(unittest.TestCase):
    """Real registration through `core.endpoint` in a fresh interpreter."""

    def _run(self, code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, **(env or {})}, check=False)

    def test_resolvers_env_registers_the_scaffold_selector_fields(self) -> None:
        code = (
            f"import sys; sys.path.insert(0, {str(SUBSTRATE)!r})\n"
            "from core.endpoint import selector_fields, registered_resolvers\n"
            "print(sorted(selector_fields())); print([r.name for r in registered_resolvers()])\n"
        )
        proc = self._run(code, {"REMOTE_DEV_RESOLVERS": remote_dev.resolver_spec()})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields, names = proc.stdout.strip().splitlines()
        self.assertEqual(fields, str(sorted(["host", "port", "alias", "machine", "session_id", "session_file"])))
        self.assertEqual(names, "['vaws']")

    def test_machine_selector_resolves_from_a_fake_inventory_with_runtime_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".vaws-local").mkdir(parents=True)
            (repo / ".vaws-local" / "machine-inventory.json").write_text(json.dumps({
                "schema_version": 1,
                "machines": [{"alias": "host-a", "host": {"ip": "203.0.113.10", "port": 22, "user": "root"},
                              "container": {"name": "vaws-test", "ssh_port": 46000, "runtime_root": "/vllm-workspace"}}],
            }), encoding="utf-8")
            code = (
                f"import sys; sys.path.insert(0, {str(SUBSTRATE)!r}); sys.path.insert(0, {str(LIB)!r})\n"
                "import json, pathlib, vaws_remote_dev_plugin as plugin\n"
                f"plugin.REPO_ROOT = pathlib.Path({str(repo)!r})\n"
                "plugin.setup()\n"
                "from core.endpoint import resolve_endpoint, has_selector\n"
                "ep = resolve_endpoint({'machine': 'host-a', 'root': '/vllm-workspace'})\n"
                "t = ep.to_result_target(); t.pop('source'); t['has_selector'] = has_selector({'machine': 'x'})\n"
                "print(json.dumps(t, sort_keys=True))\n"
            )
            proc = self._run(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        target = json.loads(proc.stdout)
        self.assertEqual((target["host"], target["port"], target["user"]), ("203.0.113.10", 46000, "root"))
        self.assertEqual(target["root"], "/vllm-workspace")
        self.assertEqual(target["cwd"], "/vllm-workspace")
        self.assertEqual(target["kind"], "managed-machine")
        self.assertEqual(target["alias"], "host-a")
        self.assertEqual(target["runtime_env_file"], remote_dev.ASCEND_RUNTIME_ENV_FILE)
        self.assertTrue(target["has_selector"])

    def test_empty_payload_without_binding_yields_the_substrate_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = (
                f"import sys; sys.path.insert(0, {str(SUBSTRATE)!r}); sys.path.insert(0, {str(LIB)!r})\n"
                "import pathlib, vaws_remote_dev_plugin as plugin\n"
                f"plugin.REPO_ROOT = pathlib.Path({tmp!r})\n"
                "plugin.setup()\n"
                "from core.endpoint import resolve_endpoint\n"
                "from core.errors import EndpointError\n"
                "try:\n    resolve_endpoint({})\nexcept EndpointError as exc:\n    print(str(exc))\n"
            )
            proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no endpoint target", proc.stdout)
        self.assertIn("registered resolvers: vaws", proc.stdout)


if __name__ == "__main__":
    unittest.main()
