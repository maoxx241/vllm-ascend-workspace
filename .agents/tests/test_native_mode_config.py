"""Native preference plans preserve user configuration and unsupported clients."""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from vaws_native_mode_config import add_native_mode, grok_native_defaults, kimi_session_setup_capability


class NativeModeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.user_dir = Path(self.temporary.name)
        self.project = self.user_dir / "project"
        self.path = self.user_dir / ".grok/config.toml"

    def plan(self, text=None):
        files, notes = {}, []
        if text is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
        add_native_mode(files, notes, "grok", self.project, user_home=self.user_dir)
        return files, notes

    def test_missing_configuration_is_planned_without_writes(self):
        files, notes = self.plan()
        parsed = tomllib.loads(files[self.path])
        self.assertEqual(parsed["cli"]["worktree_type"], "git")
        self.assertEqual(parsed["hints"], {"new_session_worktree_mode": "always", "fork_worktree_mode": "always"})
        self.assertFalse(self.path.exists())
        self.assertFalse((self.project / ".grok/config.toml").exists())
        self.assertEqual(notes[0]["scope"], "user")

    def test_custom_settings_comments_and_hooks_survive(self):
        before = ('# personal setup\n[cli]\nworktree_type = "standalone" # old choice\n'
                  'custom = "keep"\n[hints]\nnew_session_worktree_mode = "never"\n'
                  'fork_worktree_mode = "ask"\nmemory_modal_fullscreen = true\n'
                  '[[hooks.SessionStart]]\nmatcher = "startup"\n')
        files, _ = self.plan(before)
        parsed = tomllib.loads(files[self.path])
        self.assertEqual(parsed["cli"]["custom"], "keep")
        self.assertTrue(parsed["hints"]["memory_modal_fullscreen"])
        self.assertEqual(parsed["hooks"], tomllib.loads(before)["hooks"])
        self.assertTrue(files[self.path].startswith("# personal setup\n"))
        self.assertEqual(self.path.read_text(), before)

    def test_existing_plan_is_used_and_repeated_planning_is_idempotent(self):
        files, notes = self.plan('[cli]\ncustom = "from-file"\n')
        files[self.path] += '\n[models]\ndefault = "custom-model"\n'
        before = files.copy()
        add_native_mode(files, notes, "grok", self.project, user_home=self.user_dir)
        self.assertEqual(files, before)
        self.assertEqual(notes[-1]["action"], "configured")

    def test_multiline_text_containing_table_headers_is_preserved(self):
        before = 'banner = """\n[cli]\npretend = true\n"""\n[hints]\ncustom = 42\n'
        files, _ = self.plan(before)
        parsed = tomllib.loads(files[self.path])
        self.assertEqual(parsed["banner"], tomllib.loads(before)["banner"])
        self.assertEqual(parsed["hints"]["custom"], 42)

    def test_valid_inline_table_is_left_for_its_owner(self):
        before = 'cli = {worktree_type = "standalone", custom = 1}\n'
        files, notes = self.plan(before)
        self.assertEqual(files, {})
        self.assertEqual(notes[0]["action"], "preserved")
        self.assertEqual(self.path.read_text(), before)

    def test_malformed_configuration_is_not_overwritten(self):
        files, notes = self.plan('[cli\nworktree_type = "git"\n')
        self.assertEqual(files, {})
        self.assertEqual(notes[0]["reason"], "native-mode-config-needs-integration")

    def test_config_directory_override_is_honored(self):
        custom = self.user_dir / "profile"
        files, notes = {}, []
        with patch.dict(os.environ, {"GROK_HOME": str(custom)}):
            add_native_mode(files, notes, "grok", self.project)
        self.assertIn(custom / "config.toml", files)

    def test_claude_reports_native_cli_gap_without_unknown_keys(self):
        files, notes = {}, []
        add_native_mode(files, notes, "claude", self.project, user_home=self.user_dir)
        self.assertFalse(files)
        self.assertEqual(notes[0]["reason"], "no-native-default-worktree-setting")
        self.assertEqual(notes[0]["scope"], "native-cli")

    def test_kimi_does_not_gain_an_unproven_hook(self):
        files, notes = {}, []
        add_native_mode(files, notes, "kimi", self.project, user_home=self.user_dir)
        self.assertFalse(files)
        self.assertFalse(notes)


class KimiCapabilityTests(unittest.TestCase):
    def test_installed_parser_contract_in_isolated_home(self):
        paths = []

        def doctor(argv, **kwargs):
            path = Path(argv[-1])
            paths.append(path)
            self.assertEqual(argv[:3], ["/tools/kimi", "doctor", "config"])
            self.assertEqual(kwargs["cwd"], path.parent)
            self.assertEqual(kwargs["env"]["KIMI_CODE_HOME"], str(path.parent))
            event = tomllib.loads(path.read_text())["hooks"][0]["event"]
            return subprocess.CompletedProcess(argv, 0 if event == "SessionSetup" else 1,
                                               "OK" if event == "SessionSetup" else "",
                                               '' if event == "SessionSetup" else
                                               'hooks[0].event: Invalid option: expected one of "SessionSetup"|"SessionStart"')

        with patch("vaws_native_mode_config.subprocess.run", side_effect=doctor):
            result = kimi_session_setup_capability("/tools/kimi")
        self.assertTrue(result["supported"])
        self.assertEqual(len(result["checks"]), 2)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_official_or_permissive_parser_is_not_extension_evidence(self):
        for responses in ((1, 1), (0, 0)):
            with self.subTest(responses=responses), patch("vaws_native_mode_config.subprocess.run",
                    side_effect=[subprocess.CompletedProcess([], code, "", 'hooks[0].event: expected one of "SessionStart"')
                                 for code in responses]):
                self.assertFalse(kimi_session_setup_capability("kimi")["supported"])

    def test_unrelated_negative_failure_is_not_extension_evidence(self):
        with patch("vaws_native_mode_config.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0, "OK", ""),
                subprocess.CompletedProcess([], 1, "", "SessionSetup: network unavailable")]):
            self.assertFalse(kimi_session_setup_capability("kimi")["supported"])

    def test_missing_or_timed_out_cli_returns_evidence(self):
        for error in (FileNotFoundError("kimi"), subprocess.TimeoutExpired(["kimi", "doctor"], 10)):
            with self.subTest(error=error), patch("vaws_native_mode_config.subprocess.run", side_effect=error):
                result = kimi_session_setup_capability("kimi")
                self.assertFalse(result["supported"])
                self.assertEqual(result["reason"], "native-session-setup-probe-failed")
                self.assertTrue(result["error"])


class GrokInstalledEvidenceTests(unittest.TestCase):
    def test_acceptance_is_bound_to_the_installed_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            binary = project / "grok"
            binary.write_bytes(b"accepted native build")
            receipt = project / ".vaws-local/client-installations/grok-native.json"
            self.assertFalse(grok_native_defaults(binary, project)["supported"])
            receipt.parent.mkdir(parents=True)
            record = {"binary": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                      "capabilities": {"bare_worktree_default": True}}
            receipt.write_text(json.dumps(record))
            self.assertTrue(grok_native_defaults(binary, project)["supported"])
            binary.write_bytes(b"different build with the same version")
            result = grok_native_defaults(binary, project)
            self.assertFalse(result["supported"])
            self.assertEqual(result["reason"], "native-default-build-changed")

    def test_another_executable_or_incomplete_record_is_not_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            binary = project / "grok"
            binary.write_bytes(b"a native build")
            receipt = project / ".vaws-local/client-installations/grok-native.json"
            receipt.parent.mkdir(parents=True)
            for record in ({"binary": str(project / "other"), "capabilities": {"bare_worktree_default": True}},
                           {"binary": str(binary), "capabilities": {}},
                           {"binary": str(binary), "capabilities": []}, []):
                with self.subTest(record=record):
                    receipt.write_text(json.dumps(record))
                    self.assertFalse(grok_native_defaults(binary, project)["supported"])


if __name__ == "__main__":
    unittest.main()
