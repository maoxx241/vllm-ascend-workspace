"""Scaffold-side contract with the installed vaws-remote-dev package.

Generic remote-dev tools stay explicit host/port. This workspace does not
inject a VAWS resolver.
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

requires_package = unittest.skipUnless(
    importlib.util.find_spec("remote_dev") is not None,
    "vaws-remote-dev is not installed; run `uv sync`",
)


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
    def test_workspace_does_not_guess_task_from_cwd(self) -> None:
        import vaws_session_id
        self.assertFalse(hasattr(vaws_session_id, "find_session_binding"))
        self.assertFalse(hasattr(vaws_session_id, "resolve_session_id"))


class ResolverPluginMappingTests(unittest.TestCase):
    def test_resolver_plugin_is_deleted(self) -> None:
        self.assertFalse((ROOT / ".agents/lib/vaws_remote_dev_plugin.py").is_file())


class PackageWiringTests(unittest.TestCase):
    def test_substrate_environment_strips_resolvers(self) -> None:
        env = remote_dev.substrate_environment({
            "REMOTE_DEV_RESOLVERS": ".agents/lib/vaws_remote_dev_plugin.py:setup",
            "REMOTE_DEV_STATE_DIR": ".vaws-local/remote-dev-state",
        })
        self.assertNotIn("REMOTE_DEV_RESOLVERS", env)
        self.assertEqual(env["REMOTE_DEV_STATE_DIR"], str(ROOT / ".vaws-local/remote-dev-state"))
        self.assertNotIn("REMOTE_DEV_DEFAULT_ROOT", env)

    def test_package_status_names_the_lock(self) -> None:
        status = remote_dev.package_status()
        self.assertEqual(status["name"], "vaws-remote-dev")
        self.assertIn(status["state"], {"missing", "off_spec", "ready"})
        self.assertEqual(status["remedy"], "python .agents/scripts/vaws_deps.py sync")
        self.assertNotIn("resolver", status)


class ClientConfigurationTests(unittest.TestCase):
    SERVER_ARGS = ["-m", "remote_dev.mcp.server"]
    TASK_ARGS = ["-m", "vaws_coordinator", "task-server"]
    REQUIRED_ENV = ("REMOTE_DEV_DEFAULT_USER", "REMOTE_DEV_STATE_DIR")
    FORBIDDEN_ENV = ("REMOTE_DEV_RESOLVERS", "REMOTE_DEV_RUNTIME_ENV_FILE")

    def test_tracked_json_clients_use_the_package_and_inject_the_environment(self) -> None:
        for relative in (".mcp.json", ".cursor/mcp.json"):
            with self.subTest(file=relative):
                servers = json.loads((ROOT / relative).read_text(encoding="utf-8"))["mcpServers"]
                entry = servers["remote-dev"]
                self.assertEqual(entry["args"], self.SERVER_ARGS)
                self.assertTrue(entry["command"].endswith(".venv/bin/python") or entry["command"].endswith("python"), entry["command"])
                self.assertEqual(servers["vaws-task"]["args"], self.TASK_ARGS)
                for key in self.REQUIRED_ENV:
                    self.assertIn(key, entry["env"])
                for key in self.FORBIDDEN_ENV:
                    self.assertNotIn(key, entry["env"])

    def test_tracked_toml_examples_use_the_package_and_inject_the_environment(self) -> None:
        for relative, server in ((".codex/config.example.toml", "remote_dev"), (".grok/config.example.toml", "remote-dev")):
            with self.subTest(file=relative):
                data = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
                entry = data["mcp_servers"][server]
                self.assertEqual(entry["args"], self.SERVER_ARGS)
                task = data["mcp_servers"].get("vaws_task") or data["mcp_servers"]["vaws-task"]
                self.assertEqual(task["args"], self.TASK_ARGS)
                for key in self.REQUIRED_ENV:
                    self.assertIn(key, entry["env"])
                for key in self.FORBIDDEN_ENV:
                    self.assertNotIn(key, entry["env"])

    def test_claude_and_codex_hooks_use_the_package_guards(self) -> None:
        settings = json.loads((ROOT / ".claude/settings.example.json").read_text(encoding="utf-8"))
        commands = [hook["command"] for group in settings["hooks"]["PreToolUse"] for hook in group["hooks"]]
        self.assertTrue(commands)
        self.assertTrue(all("remote_dev.hooks.claude_remote_guard" in command for command in commands), commands)
        codex = (ROOT / ".codex/config.example.toml").read_text(encoding="utf-8")
        self.assertIn("remote_dev.hooks.codex_remote_guard", codex)
        self.assertNotIn(".agents/scripts/remote_dev.py", settings.__class__.__name__ or "")
        self.assertNotIn("remote_dev.py", json.dumps(settings))
        self.assertNotIn(".agents/scripts/remote_dev.py", codex)

    def test_client_setup_emits_package_entry_with_environment(self) -> None:
        setup = load_script("vaws_client_setup")
        setup.managed_python = lambda: sys.executable
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            files = setup.configuration("claude", project)
            servers = json.loads(files[project / ".mcp.json"])["mcpServers"]
            mcp = servers["remote-dev"]
            self.assertEqual(set(servers), {"remote-dev", "vaws-task", "vaws-knowledge"})
            self.assertEqual(mcp["args"], self.SERVER_ARGS)
            self.assertEqual(servers["vaws-task"]["args"], self.TASK_ARGS)
            for key in self.REQUIRED_ENV:
                self.assertIn(key, mcp["env"])
            codex = tomllib.loads(setup.configuration("codex", project)[project / ".codex/config.toml"])
            self.assertEqual(codex["mcp_servers"]["remote_dev"]["args"], self.SERVER_ARGS)
            self.assertEqual(codex["mcp_servers"]["vaws_task"]["args"], self.TASK_ARGS)
            self.assertNotIn("REMOTE_DEV_RESOLVERS", codex["mcp_servers"]["remote_dev"]["env"])
        self.assertFalse(str(setup.BACKUP_DIR).startswith(str(ROOT / ".remote-dev")))
        self.assertTrue(str(setup.BACKUP_DIR).startswith(str(ROOT / ".vaws-local")))

    def test_client_setup_keeps_user_environment_values(self) -> None:
        setup = load_script("vaws_client_setup")
        setup.managed_python = lambda: sys.executable
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote-dev": {"env": {"REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/custom.sh"}}}}))
            mcp = json.loads(setup.configuration("claude", project)[project / ".mcp.json"])["mcpServers"]["remote-dev"]
            self.assertNotIn("REMOTE_DEV_RESOLVERS", mcp["env"])


OLD_SUBSTRATE_PATH = ".remote-dev/"


class NoInTreeSubstrateTests(unittest.TestCase):
    """The old vendored copy is gone and nothing executable still reads it."""

    GUARD_RUNTIME_EXCLUSION_FILE = ".agents/lib/vaws_leak_guard.py"
    RETAINED_RUNTIME_STATE_GLOB = ".remote-dev/state/**"
    EXCLUDED_GLOBS_NAME = "DEFAULT_EXCLUDED_PATH_GLOBS"

    def test_in_tree_copy_is_deleted(self) -> None:
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", ".remote-dev"], capture_output=True, text=True, check=False)
        self.assertEqual(tracked.stdout.strip(), "", "the in-tree .remote-dev copy must not be tracked")

    @staticmethod
    def _docstring_constant_ids(tree: ast.AST) -> set[int]:
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body:
                first = body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
        return docstrings

    @classmethod
    def _retained_runtime_state_exclusion_ids(cls, relative: str, tree: ast.AST) -> set[int]:
        """The one declared runtime-data skip glob is not a vendored-code fallback."""

        if relative != cls.GUARD_RUNTIME_EXCLUSION_FILE:
            return set()
        retained: set[int] = set()
        for stmt in getattr(tree, "body", ()):
            names: list[str] = []
            value = None
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names = [stmt.target.id]
                value = stmt.value
            elif isinstance(stmt, ast.Assign):
                names = [target.id for target in stmt.targets if isinstance(target, ast.Name)]
                value = stmt.value
            if cls.EXCLUDED_GLOBS_NAME not in names:
                continue
            if not isinstance(value, (ast.Tuple, ast.List)):
                continue
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and elt.value == cls.RETAINED_RUNTIME_STATE_GLOB:
                    retained.add(id(elt))
        return retained

    @classmethod
    def _python_string_constants(cls, source: str) -> list[str]:
        """String constants with module/class/function docstrings excluded.

        Comments never reach the AST and docstrings may explain history; only
        a string a program can act on counts as a reference.
        """
        tree = ast.parse(source)
        docstrings = cls._docstring_constant_ids(tree)
        return [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]

    @classmethod
    def _actionable_python_strings(cls, relative: str, source: str) -> list[str]:
        """Non-docstring literals, minus the one retained guard runtime skip glob."""

        tree = ast.parse(source)
        skip = cls._docstring_constant_ids(tree)
        skip |= cls._retained_runtime_state_exclusion_ids(relative, tree)
        return [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip]

    def test_no_tracked_code_or_config_references_the_old_path(self) -> None:
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=False).stdout.split("\0")
        exempt = {
            ".agents/tests/test_remote_dev_consumer.py",
            ".agents/tests/test_repo_boundary_check.py",  # names the old path as a guard fixture
            # The boundary policy and its baseline explain *why* the coupling
            # existed; that prose is history, not a path anything reads.
            ".agents/policy/repo-boundaries.json",
            ".agents/policy/repo-boundaries-baseline.json",
            # Dated missing-path evidence for the frozen historical CLI table.
            ".agents/policy/tracked-paths-baseline.json",
            # Historical CLI census names deleted substrate paths as inventory.
            ".agents/scripts/cli_surface_inventory.py",
            ".agents/tests/fixtures/cli-surface-inventory.json",
            ".agents/tests/test_cli_surface_inventory.py",
        }
        offenders = []
        for relative in tracked:
            path = ROOT / relative
            if not relative or relative in exempt or not path.is_file():
                continue
            suffix = path.suffix
            if suffix == ".py":
                haystack = "\n".join(self._actionable_python_strings(relative, path.read_text(encoding="utf-8", errors="replace")))
            elif suffix in {".json", ".toml", ".yml", ".yaml"}:
                haystack = path.read_text(encoding="utf-8", errors="replace")
            else:
                continue
            if OLD_SUBSTRATE_PATH in haystack:
                offenders.append(relative)
        self.assertEqual(offenders, [])

    def test_exact_guard_runtime_state_exclusion_is_accepted(self) -> None:
        cases = (
            'DEFAULT_EXCLUDED_PATH_GLOBS: tuple[str, ...] = ("vllm/**", ".remote-dev/state/**")\n',
            'DEFAULT_EXCLUDED_PATH_GLOBS = ("vllm/**", ".remote-dev/state/**")\n',
            'DEFAULT_EXCLUDED_PATH_GLOBS = ["vllm/**", ".remote-dev/state/**"]\n',
        )
        for source in cases:
            with self.subTest(source=source):
                strings = self._actionable_python_strings(self.GUARD_RUNTIME_EXCLUSION_FILE, source)
                self.assertEqual(strings, ["vllm/**"])

    def test_core_path_in_the_same_globs_declaration_is_rejected(self) -> None:
        source = (
            "DEFAULT_EXCLUDED_PATH_GLOBS: tuple[str, ...] = (\n"
            '    ".remote-dev/state/**",\n'
            '    ".remote-dev/core/endpoint.py",\n'
            ")\n"
        )
        strings = self._actionable_python_strings(self.GUARD_RUNTIME_EXCLUSION_FILE, source)
        self.assertIn(".remote-dev/core/endpoint.py", strings)
        self.assertNotIn(self.RETAINED_RUNTIME_STATE_GLOB, strings)

    def test_same_state_literal_in_another_assignment_or_call_is_rejected(self) -> None:
        source = (
            "DEFAULT_EXCLUDED_PATH_GLOBS: tuple[str, ...] = (\n"
            '    "vllm/**",\n'
            ")\n"
            'OTHER = ".remote-dev/state/**"\n'
            'skip(".remote-dev/state/**")\n'
        )
        strings = self._actionable_python_strings(self.GUARD_RUNTIME_EXCLUSION_FILE, source)
        self.assertEqual(strings.count(self.RETAINED_RUNTIME_STATE_GLOB), 2)

    def test_same_state_literal_in_another_source_path_is_rejected(self) -> None:
        source = (
            "DEFAULT_EXCLUDED_PATH_GLOBS: tuple[str, ...] = (\n"
            '    ".remote-dev/state/**",\n'
            ")\n"
        )
        strings = self._actionable_python_strings(".agents/scripts/tracked_leak_scan.py", source)
        self.assertIn(self.RETAINED_RUNTIME_STATE_GLOB, strings)

    def test_old_launcher_source_fallback_in_the_guard_file_is_rejected(self) -> None:
        source = (
            "DEFAULT_EXCLUDED_PATH_GLOBS: tuple[str, ...] = (\n"
            '    ".remote-dev/state/**",\n'
            ")\n"
            'WORKER = ROOT / ".remote-dev/core/managed_jobs.py"\n'
        )
        strings = self._actionable_python_strings(self.GUARD_RUNTIME_EXCLUSION_FILE, source)
        self.assertIn(".remote-dev/core/managed_jobs.py", strings)
        self.assertNotIn(self.RETAINED_RUNTIME_STATE_GLOB, strings)

    def test_docstring_old_path_is_still_not_a_reference(self) -> None:
        source = (
            '"""Historical in-tree path .remote-dev/core/managed_jobs.py."""\n'
            "DEFAULT_EXCLUDED_PATH_GLOBS: tuple[str, ...] = (\n"
            '    ".remote-dev/state/**",\n'
            ")\n"
        )
        self.assertEqual(self._python_string_constants(source), [self.RETAINED_RUNTIME_STATE_GLOB])
        self.assertEqual(self._actionable_python_strings(self.GUARD_RUNTIME_EXCLUSION_FILE, source), [])

    def test_vaws_cli_reports_moved_task_tools_without_traceback(self) -> None:
        python = str(Path(sys.base_prefix) / "bin" / "python3")
        if not Path(python).is_file() or Path(python).resolve() == Path(sys.executable).resolve():
            self.skipTest("no Python 3.11+ interpreter outside .venv")
        drop = {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in drop and not key.startswith("UV_")
        }
        env["VAWS_SKIP_VENV_REEXEC"] = "1"
        proc = subprocess.run(
            [python, str(SCRIPTS / "vaws.py"), "session", "--json", "{}"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout)["result"]
        self.assertEqual((payload["tool"], payload["outcome"], payload["status"]), ("vaws.session", "blocked", "unavailable"))
        self.assertIn("vaws-coordinator", payload["summary"] + json.dumps(payload))
        self.assertIn("vaws_deps.py sync", payload["summary"] + json.dumps(payload) + proc.stderr)

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



if __name__ == "__main__":
    unittest.main()
