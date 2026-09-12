#!/usr/bin/env python3
"""Hermetic probe/decision tests for repo-init.

Git and gh are injected through ``probe.run``. No GitHub API, no initialized
submodule, and no developer HOME are required.

Dependency installation is handled by its owner, not a field on the probe
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
    def test_low_level_configure_cannot_bypass_verified_fork_setup(self) -> None:
        args = SimpleNamespace(repo=".", origin_url="https://github.com/an-org/vllm.git", upstream_url=None)
        with mock.patch.object(topology, "resolve_repo", return_value=Path("/unused")), \
             mock.patch.object(topology, "mutate_remote") as mutate:
            with self.assertRaisesRegex(topology.RepoTopologyError, "workspace_forks.py"):
                topology.cmd_configure(args)
            mutate.assert_not_called()

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
                "owner_login": "alice",
                "owner_type": "User",
                "parent_full_name": "vllm-project/vllm",
            },
            "alice",
        )
        self.assertTrue(personal["personal_fork"])
        self.assertEqual(personal["classification"], "user-fork")

    def test_personal_fork_requires_personal_owner_and_official_network(self) -> None:
        valid = {"exists": True, "full_name": "alice/vllm", "is_fork": True,
                 "owner_login": "alice", "owner_type": "User",
                 "parent_full_name": "vllm-project/vllm"}
        for changed in ({"is_fork": False}, {"owner_type": "Organization"},
                        {"parent_full_name": "unrelated/vllm"}, {"owner_login": "another-user"}):
            with self.subTest(changed=changed):
                result = probe.personal_fork_record("vllm", {**valid, **changed}, "alice")
                self.assertFalse(result["personal_fork"])
                self.assertIn("policy_error", result)


class ProbeSummaryTests(unittest.TestCase):
    def test_compact_summary_is_read_only_and_has_no_setup_decisions(self) -> None:
        from unittest.mock import patch
        payload = {
            "platform": {"kind": "windows", "machine": "AMD64"},
            "gh": {"installed": True, "logged_in": True, "user_login": "alice"},
            "submodules": [{"state": "-", "path": "vllm"}],
            "repos": {},
        }
        with patch.object(probe, "run", side_effect=AssertionError("summary must not probe again")):
            compact = probe.compact_payload(payload)
        self.assertFalse(compact["submodules"]["all_initialized"])
        self.assertEqual(compact["gh"]["user_login"], "alice")
        self.assertNotIn("decision_checkpoint", compact)
        self.assertNotIn("workspace_identity", compact)
        self.assertNotIn("workspace_profile", compact)

    def test_real_git_feeds_initialized_clean_first_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import subprocess

            root = Path(tmp) / "workspace"
            source = Path(tmp) / "module source"
            def git(path, *args):
                subprocess.run(
                    ["git", "-c", "protocol.file.allow=always", "-c", "user.name=Probe Test",
                     "-c", "user.email=probe@example.invalid", "-c", "core.hooksPath=/dev/null",
                     "-C", str(path), *args],
                    capture_output=True, check=True,
                )
            for path in (root, source):
                path.mkdir()
                git(path, "init")
                git(path, "commit", "--allow-empty", "-m", "fixture")
            for name in ("vllm", "vllm-ascend"):
                git(root, "submodule", "add", str(source), name)
            rows = probe.git_submodule_status(root)
            self.assertEqual(
                [(row["state"], row["path"]) for row in rows],
                [(" ", "vllm"), (" ", "vllm-ascend")],
            )
            self.assertTrue(probe.compact_submodule_summary(rows)["all_initialized"])


if __name__ == "__main__":
    unittest.main()
