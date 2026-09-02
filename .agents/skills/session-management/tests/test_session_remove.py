#!/usr/bin/env python3
"""Tests for bounded session worktree cleanup."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "session-management"


def load_module():
    name = "_session_remove_test"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / "session_remove.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


session_remove = load_module()
gc_spec = importlib.util.spec_from_file_location("_session_gc_test", SKILL / "scripts" / "session_gc.py")
session_gc = importlib.util.module_from_spec(gc_spec)
gc_spec.loader.exec_module(session_gc)


class SessionRemoveTests(unittest.TestCase):
    def test_worktree_removal_cannot_release_live_remote_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lookup = SimpleNamespace(
                session={"session_id": "task", "local": {"worktree_root": tmp}},
                session_file=Path(tmp) / "session.json", state_repo_root=Path(tmp),
            )
            for stop_status, expected in (("failed", "needs_repair"), ("stopped", "stopped")):
                with self.subTest(stop_status=stop_status), mock.patch.object(session_remove, "load_session_lookup", return_value=lookup), mock.patch.object(session_remove, "stop_session", return_value={"status": stop_status, "returncode": int(stop_status == "failed")}), mock.patch.object(session_remove, "worktree_is_clean", return_value=True), mock.patch.object(session_remove, "remove_session_worktree", return_value={"returncode": 0}), mock.patch.object(session_remove, "release_all_session_leases") as release, mock.patch.object(session_remove, "mark_session_status", return_value={"status": expected}) as mark, mock.patch.object(sys, "argv", ["session_remove.py", "--session-id", "task", "--remove-worktree"]), mock.patch("builtins.print"):
                    session_remove.main()
                release.assert_not_called()
                self.assertEqual(mark.call_args.kwargs["status"], expected)

    def test_ssh_failures_are_not_proof_of_dead_container(self) -> None:
        for stderr in ("Connection refused", "Permission denied", "No route to host", "Connection timed out"):
            with self.subTest(stderr=stderr), mock.patch.object(session_gc.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", stderr)):
                self.assertIsNone(session_gc.probe_container_alive("host", 46000)["alive"])

    def test_gc_retains_lease_when_metadata_is_missing_or_removed(self) -> None:
        lease = {"leases": {"host": {"npu_devices": {"0": {"session_id": "task"}}}}}
        for missing in (True, False):
            lookup = SimpleNamespace(session={"session_id": "task", "status": "removed"}, state_repo_root=Path("/tmp/state"))
            with self.subTest(missing=missing), mock.patch.object(session_gc, "load_index", return_value={"sessions": {"task": {}}}), mock.patch.object(session_gc, "load_leases", return_value=lease), mock.patch.object(session_gc, "load_session_lookup", side_effect=ValueError("missing metadata") if missing else None, return_value=lookup), mock.patch.object(session_gc, "release_all_session_leases") as release, mock.patch.object(sys, "argv", ["session_gc.py", "--apply"]), mock.patch("builtins.print"):
                self.assertEqual(session_gc.main(), 0)
            release.assert_not_called()

    def test_gc_host_confirmation_is_required_and_uncertainty_keeps_lease(self) -> None:
        session = {"remote": {"host": "host", "host_port": 22, "container": {"name": "task-container"}}, "leases": {"npu_devices": [0]}}
        for result, expected in ((subprocess.CompletedProcess([], 255, "", "Permission denied"), None), (subprocess.CompletedProcess([], 0, json.dumps({"alive": False}), ""), False)):
            with mock.patch.object(session_gc.subprocess, "run", return_value=result) as call:
                self.assertIs(session_gc._probe_session_container(session)["alive"], expected)
            self.assertIn("22", call.call_args.args[0])
            self.assertIn("_confirmed_free_probe", call.call_args.args[0][-1])

    def test_remote_cleanup_exception_marks_session_needs_repair(self) -> None:
        lookup = SimpleNamespace(
            session={"session_id": "cleanup-session"},
            session_file=Path("/tmp/session.json"),
            state_repo_root=Path("/tmp/state"),
        )
        with (
            mock.patch.object(
                session_remove,
                "load_session_lookup",
                return_value=lookup,
            ),
            mock.patch.object(
                session_remove,
                "session_serving_state_path",
                return_value=Path("/definitely/missing/serving.json"),
            ),
            mock.patch.object(
                session_remove,
                "session_record_for_execution",
                return_value={},
            ),
            mock.patch.object(
                session_remove,
                "remove_container",
                side_effect=RuntimeError("host unreachable"),
            ),
            mock.patch.object(
                session_remove,
                "mark_session_status",
                return_value={"status": "needs_repair"},
            ) as mark_status,
            mock.patch.object(
                sys,
                "argv",
                [
                    "session_remove.py",
                    "--session-id",
                    "cleanup-session",
                    "--remove-container",
                    "--release-leases",
                ],
            ),
            mock.patch("builtins.print"),
        ):
            returncode = session_remove.main()

        self.assertEqual(returncode, 2)
        mark_status.assert_called_once_with(
            repo_root=lookup.state_repo_root,
            session_id="cleanup-session",
            status="needs_repair",
        )

    def test_deinitializes_submodules_before_removing_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            calls: list[tuple[Path, list[str]]] = []

            def runner(args: list[str], *, cwd: Path, check: bool = False):
                del check
                calls.append((cwd, args))
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="ok",
                    stderr="",
                )

            result = session_remove.remove_session_worktree(
                worktree,
                force=False,
                runner=runner,
            )

            self.assertEqual(
                calls,
                [
                    (worktree, ["submodule", "deinit", "--force", "--all"]),
                    (
                        session_remove.ROOT,
                        ["worktree", "remove", str(worktree)],
                    ),
                ],
            )
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["submodule_deinit"]["returncode"], 0)

    def test_force_is_forwarded_to_worktree_removal(self) -> None:
        missing = Path("/definitely/missing/session-worktree")
        calls: list[tuple[Path, list[str]]] = []

        def runner(args: list[str], *, cwd: Path, check: bool = False):
            del check
            calls.append((cwd, args))
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="",
                stderr="",
            )

        result = session_remove.remove_session_worktree(
            missing,
            force=True,
            runner=runner,
        )

        self.assertEqual(
            calls,
            [
                (
                    session_remove.ROOT,
                    [
                        "worktree",
                        "remove",
                        "--force",
                        "--force",
                        str(missing),
                    ],
                )
            ],
        )
        self.assertIsNone(result["submodule_deinit"])

    def test_ignored_evidence_files_make_worktree_unclean(self) -> None:
        # `.vaws-local/` run evidence is Git-ignored; a clean check without
        # `--ignored` would let `worktree remove` silently destroy it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", "-C", str(root), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (root / ".gitignore").write_text(".vaws-local/\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")

            self.assertTrue(session_remove.worktree_is_clean(root))

            evidence = root / ".vaws-local"
            evidence.mkdir()
            (evidence / "run-manifest.json").write_text("{}", encoding="utf-8")

            self.assertFalse(session_remove.worktree_is_clean(root))


if __name__ == "__main__":
    unittest.main()
