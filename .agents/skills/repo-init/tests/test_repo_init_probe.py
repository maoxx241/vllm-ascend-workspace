#!/usr/bin/env python3
"""Hermetic probe/decision tests for repo-init.

Git and gh are injected through ``probe.run``. No GitHub API, no initialized
submodule, and no developer HOME are required.

``uv sync`` is a SKILL.md checkpoint question, not a field on the probe
payload. These tests cover the required-step reporting the probe actually
emits (topology + submodule initialization).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "repo-init" / "scripts"
LIB = ROOT / ".agents" / "lib"
for path in (str(LIB), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import repo_init_probe as probe  # noqa: E402
import repo_topology as topology  # noqa: E402


class SubmoduleClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._run = probe.run

    def tearDown(self) -> None:
        probe.run = self._run  # type: ignore[method-assign]

    def test_git_submodule_status_classifies_initialized_missing_and_dirty(self) -> None:
        def fake_run(cmd: list[str], cwd: Path | None = None):
            if cmd[:2] == ["git", "submodule"]:
                return (
                    0,
                    "  abcdef1 vllm (heads/main)\n"
                    "- 0000000 vllm-ascend\n"
                    "+ fedcba9 extra",
                    "",
                )
            return 1, "", "unexpected"

        probe.run = fake_run  # type: ignore[method-assign]
        rows = probe.git_submodule_status(Path("/tmp/unused-root"))
        self.assertEqual(
            [(row["state"], row["path"]) for row in rows],
            [(" ", "vllm"), ("-", "vllm-ascend"), ("+", "extra")],
        )
        summary = probe.compact_submodule_summary(rows)
        self.assertEqual(summary["count"], 3)
        self.assertFalse(summary["all_initialized"])
        self.assertEqual(
            [item["path"] for item in summary["needs_attention"]],
            ["vllm-ascend", "extra"],
        )

    def test_all_initialized_requires_space_prefixed_rows(self) -> None:
        self.assertFalse(probe.compact_submodule_summary([])["all_initialized"])
        ready = probe.compact_submodule_summary(
            [
                {"state": " ", "path": "vllm", "detail": ""},
                {"state": " ", "path": "vllm-ascend", "detail": ""},
            ]
        )
        self.assertTrue(ready["all_initialized"])
        self.assertEqual(ready["needs_attention"], [])

    def test_submodule_inspect_error_is_surfaced(self) -> None:
        probe.run = lambda cmd, cwd=None: (1, "", "fatal: not a git repository")  # type: ignore[method-assign]
        rows = probe.git_submodule_status(Path("/tmp/unused-root"))
        self.assertEqual(rows[0]["error"], "fatal: not a git repository")
        summary = probe.compact_submodule_summary(rows)
        self.assertFalse(summary["all_initialized"])
        self.assertEqual(summary["needs_attention"][0]["error"], "fatal: not a git repository")

    def test_run_keeps_leading_space_on_initialized_clean_first_row(self) -> None:
        raw = " aaaaaaaa vllm (v0.11.0)\n fedcba90 vllm-ascend (heads/main)\n"
        completed = SimpleNamespace(returncode=0, stdout=raw, stderr="")
        with mock.patch.object(probe.subprocess, "run", return_value=completed):
            rc, out, _err = probe.run(["git", "submodule", "status"])
            rows = probe.git_submodule_status(Path("/tmp/unused-root"))
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], " ")
        self.assertEqual(
            [(row["state"], row["path"]) for row in rows],
            [(" ", "vllm"), (" ", "vllm-ascend")],
        )
        self.assertTrue(probe.compact_submodule_summary(rows)["all_initialized"])


class ForkTopologyTests(unittest.TestCase):
    def test_parse_remote_url_accepts_ssh_https_and_rejects_other_hosts(self) -> None:
        self.assertEqual(probe.parse_remote_url("git@github.com:alice/vllm.git"), "alice/vllm")
        self.assertEqual(
            probe.parse_remote_url("https://github.com/vllm-project/vllm"),
            "vllm-project/vllm",
        )
        self.assertEqual(
            probe.parse_remote_url("ssh://git@github.com/alice/vllm-ascend.git"),
            "alice/vllm-ascend",
        )
        self.assertIsNone(probe.parse_remote_url("git@gitlab.example.invalid:alice/vllm.git"))
        self.assertEqual(topology.parse_repo_url("git@github.com:alice/vllm.git"), "alice/vllm")

    def test_inspect_repo_classifies_uninitialized_submodule_without_real_git(self) -> None:
        def fake_run(cmd: list[str], cwd: Path | None = None):
            if cmd[:2] == ["git", "rev-parse"] and "--git-dir" in cmd:
                return 0, ".git", ""
            if cmd[:2] == ["git", "rev-parse"] and "--show-toplevel" in cmd:
                return 0, "/tmp/parent-workspace", ""
            return 1, "", "unexpected"

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "vllm").mkdir()
            original = probe.run
            probe.run = fake_run  # type: ignore[method-assign]
            try:
                inspected = probe.inspect_repo(workspace, "vllm", "alice")
            finally:
                probe.run = original  # type: ignore[method-assign]
            self.assertTrue(inspected["exists"])
            self.assertFalse(inspected["initialized"])
            self.assertIn("not an initialized submodule", inspected["error"])

    def test_inspect_repo_missing_directory_is_uninitialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inspected = probe.inspect_repo(Path(tmp), "vllm", "alice")
        self.assertFalse(inspected["exists"])
        self.assertFalse(inspected["initialized"])

    def test_personal_fork_record_rejects_community_and_redirects(self) -> None:
        missing = probe.personal_fork_record(
            "vllm",
            {"exists": False, "error": "Not Found", "full_name": None},
            "alice",
        )
        self.assertEqual(missing["classification"], "missing")
        self.assertFalse(missing["personal_fork"])

        community = probe.personal_fork_record(
            "vllm",
            {
                "exists": True,
                "full_name": "vllm-project/vllm",
                "redirected": False,
                "is_fork": False,
            },
            "alice",
        )
        self.assertEqual(community["classification"], "community")
        self.assertFalse(community["personal_fork"])
        self.assertFalse(community["exists"])

        personal = probe.personal_fork_record(
            "vllm",
            {
                "exists": True,
                "full_name": "alice/vllm",
                "redirected": False,
                "is_fork": True,
                "parent_full_name": "vllm-project/vllm",
            },
            "alice",
        )
        self.assertTrue(personal["personal_fork"])
        self.assertEqual(personal["classification"], "user-fork")


class RequiredStepReportingTests(unittest.TestCase):
    def test_compact_payload_reports_required_topology_and_submodule_steps(self) -> None:
        original_detect = probe.detect_git_username_candidate
        original_question = probe.fixed_machine_username_question
        probe.detect_git_username_candidate = lambda repo_root=None: {  # type: ignore[method-assign]
            "available": False,
            "candidate": None,
            "source": None,
            "raw_value": None,
        }
        probe.fixed_machine_username_question = lambda repo_root=None: {"options": []}  # type: ignore[method-assign]
        try:
            compact = probe.compact_payload(
                {
                    "platform": {"kind": "macos", "machine": "arm64"},
                    "repo_root": None,
                    "workspace_profile": {
                        "exists": False,
                        "choice_required": True,
                        "username_rules": "letters and digits",
                        "default_generated_pattern": "agent#####",
                        "machine_username": None,
                    },
                    "workspace_identity": {"alias_choice_required": True},
                    "gh": {"installed": False, "logged_in": False},
                    "gh_install_plan": {
                        "preferred": {"label": "Homebrew"},
                        "fallback": {"label": "user-space installer"},
                    },
                    "submodules": [
                        {"state": "-", "path": "vllm", "detail": ""},
                        {"state": "-", "path": "vllm-ascend", "detail": ""},
                    ],
                    "repos": {},
                    "forks": {},
                }
            )
        finally:
            probe.detect_git_username_candidate = original_detect  # type: ignore[method-assign]
            probe.fixed_machine_username_question = original_question  # type: ignore[method-assign]
        checkpoint = compact["decision_checkpoint"]
        self.assertTrue(checkpoint["required_for_broad_init"])
        self.assertFalse(checkpoint["repo_topology"]["required"])
        self.assertEqual(
            checkpoint["repo_topology"]["options"],
            ["keep-current", "recommended-fork-mode", "community-only"],
        )
        self.assertFalse(checkpoint["submodules"]["required"])
        self.assertFalse(checkpoint["submodules"]["initialized"])
        self.assertTrue(checkpoint["machine_username"]["required"])
        self.assertTrue(checkpoint["workspace_alias"]["required"])
        self.assertEqual(checkpoint["defaults"]["uv_sync"], True)
        self.assertEqual(checkpoint["defaults"]["vllm_alignment"], "ci-pinned")

    def test_stub_git_on_path_feeds_initialized_clean_first_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "bin"
            stub.mkdir()
            git = stub / "git"
            git.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-c" ]; then shift 2; fi\n'
                'if [ "$1" = "submodule" ]; then\n'
                "  printf -- ' aaaaaaaa vllm (v0.11.0)\\n fedcba90 vllm-ascend (heads/main)\\n'\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            git.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            old_home = os.environ.get("HOME")
            os.environ["PATH"] = f"{stub}{os.pathsep}{old_path}"
            os.environ["HOME"] = tmp
            try:
                rows = probe.git_submodule_status(Path(tmp))
            finally:
                os.environ["PATH"] = old_path
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            self.assertEqual(
                [(row["state"], row["path"]) for row in rows],
                [(" ", "vllm"), (" ", "vllm-ascend")],
            )
            self.assertTrue(probe.compact_submodule_summary(rows)["all_initialized"])


if __name__ == "__main__":
    unittest.main()
