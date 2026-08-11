#!/usr/bin/env python3
"""Tests for bounded session worktree cleanup."""

from __future__ import annotations

import importlib.util
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


class SessionRemoveTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
