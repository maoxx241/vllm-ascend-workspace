from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manage_monitor.py"
SPEC = importlib.util.spec_from_file_location("npu_fleet_monitor_manager", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ManageMonitorTests(unittest.TestCase):
    def test_parse_worktrees_preserves_branch_mapping(self) -> None:
        records = MODULE.parse_worktrees(
            "worktree /repo\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /monitor\nHEAD def\nbranch refs/heads/vaws-top\n\n"
        )
        self.assertEqual(records[1]["worktree"], "/monitor")
        self.assertEqual(records[1]["branch"], "refs/heads/vaws-top")

    def test_default_branch_is_standalone_project_branch(self) -> None:
        self.assertEqual(MODULE.DEFAULT_BRANCH, "vaws-top")

    def test_missing_local_branch_tracks_origin_branch(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> mock.Mock:
            calls.append(command)
            if command == ["git", "remote"]:
                return mock.Mock(returncode=0, stdout="origin\nupstream\n", stderr="")
            if command == ["git", "show-ref", "--verify", "refs/heads/vaws-top"]:
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(MODULE, "run", side_effect=fake_run):
            MODULE.ensure_local_branch("vaws-top")

        self.assertIn(
            ["git", "branch", "--track", "vaws-top", "origin/vaws-top"],
            calls,
        )

    def test_validate_project_rejects_missing_contract(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(MODULE.MonitorError, "missing required files"):
                MODULE.validate_project(Path(root), MODULE.DEFAULT_BRANCH)

    def test_default_endpoint_is_loopback(self) -> None:
        self.assertTrue(MODULE.DEFAULT_URL.startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
