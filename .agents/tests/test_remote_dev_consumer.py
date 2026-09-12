"""Scaffold-side contract with the installed vaws-remote-dev package.

Generic remote-dev tools stay explicit host/port. This workspace does not
inject a VAWS resolver.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_remote_dev as remote_dev  # noqa: E402
from client_setup_fixtures import selected_runtime

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
        self.assertEqual(status["remedy"], "uv run --no-project python .agents/scripts/vaws_deps.py sync")
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
        with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as runtime:
            project = Path(tmp).resolve()
            selected_runtime(runtime, setup, project)
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
        with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as runtime:
            project = Path(tmp).resolve()
            selected_runtime(runtime, setup, project)
            (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote-dev": {"env": {"REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/custom.sh"}}}}))
            mcp = json.loads(setup.configuration("claude", project)[project / ".mcp.json"])["mcpServers"]["remote-dev"]
            self.assertNotIn("REMOTE_DEV_RESOLVERS", mcp["env"])


OLD_SUBSTRATE_PATH = ".remote-dev/"


class ClaudeSkillShimTests(unittest.TestCase):
    """Moved with `sync_claude_skills.py` from the substrate's test_cli_help.py."""


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
