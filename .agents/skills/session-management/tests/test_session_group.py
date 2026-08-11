#!/usr/bin/env python3
"""Tests for grouping existing isolated sessions."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
SKILL = ROOT / ".agents" / "skills" / "session-management"


def load_module():
    name = "_session_group_test"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / "session_group.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


groups = load_module()
NOW = "2026-07-25T12:00:00Z"


def write_session(
    repo: Path,
    session_id: str,
    machine: str,
    *,
    worktree: Path | None = None,
) -> None:
    path = repo / ".vaws-local" / "sessions" / session_id / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "base_machine": machine,
        "status": "ready",
        "local": {
            "worktree_root": str(worktree or repo / "worktrees" / session_id)
        },
        "remote": {
            "host": machine,
            "container": {
                "name": f"container-{session_id}",
                "ssh_port": 46000,
            },
        },
        "leases": {"npu_devices": [0]},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def same_snapshot(_session: dict) -> dict:
    return {
        "workspace_head": "abc",
        "submodules": [" def vllm", " ghi vllm-ascend"],
        "dirty": False,
    }


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def create_test_worktrees(root: Path) -> tuple[Path, Path]:
    first = root / "first"
    second = root / "second"
    first.mkdir()
    run_git(first, "init")
    run_git(first, "config", "user.name", "Session Group Test")
    run_git(first, "config", "user.email", "session-group@example.invalid")
    (first / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_git(first, "add", "tracked.txt")
    run_git(first, "commit", "-m", "base")
    run_git(first, "worktree", "add", "--detach", str(second), "HEAD")
    return first, second


def create_test_worktrees_with_submodule(root: Path) -> tuple[Path, Path]:
    child = root / "child-source"
    child.mkdir()
    run_git(child, "init")
    run_git(child, "config", "user.name", "Session Group Test")
    run_git(child, "config", "user.email", "session-group@example.invalid")
    (child / "child.txt").write_text("base-child\n", encoding="utf-8")
    run_git(child, "add", "child.txt")
    run_git(child, "commit", "-m", "child base")

    first, second = create_test_worktrees(root)
    run_git(
        first,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(child),
        "module",
    )
    run_git(first, "commit", "-am", "add submodule")
    run_git(first, "worktree", "remove", "--force", str(second))
    run_git(first, "worktree", "add", "--detach", str(second), "HEAD")
    run_git(
        second,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
    )
    return first, second


class SessionGroupTests(unittest.TestCase):
    def test_requires_two_distinct_members(self) -> None:
        with self.assertRaisesRegex(groups.SessionGroupError, "at least two"):
            groups.parse_members(["head=session-a"])
        with self.assertRaisesRegex(groups.SessionGroupError, "more than once"):
            groups.parse_members(["head=session-a", "worker=session-a"])

    def test_create_enforces_snapshot_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_session(repo, "session-a", "host-a")
            write_session(repo, "session-b", "host-b")
            calls = 0

            def different(_session: dict) -> dict:
                nonlocal calls
                calls += 1
                return {
                    "workspace_head": f"commit-{calls}",
                    "submodules": [],
                    "dirty": False,
                }

            with self.assertRaisesRegex(groups.SessionGroupError, "same workspace"):
                groups.create_group(
                    repo_root=repo,
                    group_id="pd-group",
                    member_specs=["prefill=session-a", "decode=session-b"],
                    snapshot_resolver=different,
                    created_at=NOW,
                )

    def test_rejects_same_head_with_different_tracked_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            first, second = create_test_worktrees(repo)
            (first / "tracked.txt").write_text("candidate-a\n", encoding="utf-8")
            (second / "tracked.txt").write_text("candidate-b\n", encoding="utf-8")
            write_session(repo, "session-a", "host-a", worktree=first)
            write_session(repo, "session-b", "host-b", worktree=second)

            first_snapshot = groups.workspace_snapshot(
                {"local": {"worktree_root": str(first)}}
            )
            second_snapshot = groups.workspace_snapshot(
                {"local": {"worktree_root": str(second)}}
            )
            self.assertEqual(
                first_snapshot["workspace_head"], second_snapshot["workspace_head"]
            )
            self.assertTrue(first_snapshot["dirty"])
            self.assertNotEqual(
                first_snapshot["dirty_digest"], second_snapshot["dirty_digest"]
            )
            with self.assertRaisesRegex(groups.SessionGroupError, "same workspace"):
                groups.create_group(
                    repo_root=repo,
                    group_id="dirty-tracked",
                    member_specs=["prefill=session-a", "decode=session-b"],
                )

    def test_rejects_same_head_with_different_untracked_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            first, second = create_test_worktrees(repo)
            (first / "candidate.txt").write_text("candidate-a\n", encoding="utf-8")
            (second / "candidate.txt").write_text("candidate-b\n", encoding="utf-8")
            write_session(repo, "session-a", "host-a", worktree=first)
            write_session(repo, "session-b", "host-b", worktree=second)

            with self.assertRaisesRegex(groups.SessionGroupError, "same workspace"):
                groups.create_group(
                    repo_root=repo,
                    group_id="dirty-untracked",
                    member_specs=["prefill=session-a", "decode=session-b"],
                )

    def test_accepts_same_head_with_identical_dirty_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            first, second = create_test_worktrees(repo)
            for worktree in (first, second):
                (worktree / "tracked.txt").write_text(
                    "same-candidate\n", encoding="utf-8"
                )
                (worktree / "candidate.txt").write_text(
                    "same-untracked\n", encoding="utf-8"
                )
            write_session(repo, "session-a", "host-a", worktree=first)
            write_session(repo, "session-b", "host-b", worktree=second)

            created = groups.create_group(
                repo_root=repo,
                group_id="same-dirty",
                member_specs=["prefill=session-a", "decode=session-b"],
                created_at=NOW,
            )

            self.assertEqual(created["status"], "ready")
            self.assertEqual(
                created["members"][0]["snapshot"]["dirty_digest"],
                created["members"][1]["snapshot"]["dirty_digest"],
            )

    def test_rejects_different_dirty_submodule_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            first, second = create_test_worktrees_with_submodule(repo)
            (first / "module" / "child.txt").write_text(
                "candidate-a\n", encoding="utf-8"
            )
            (second / "module" / "child.txt").write_text(
                "candidate-b\n", encoding="utf-8"
            )
            write_session(repo, "session-a", "host-a", worktree=first)
            write_session(repo, "session-b", "host-b", worktree=second)

            with self.assertRaisesRegex(groups.SessionGroupError, "same workspace"):
                groups.create_group(
                    repo_root=repo,
                    group_id="dirty-submodule",
                    member_specs=["prefill=session-a", "decode=session-b"],
                )

    def test_create_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_session(repo, "session-a", "host-a")
            write_session(repo, "session-b", "host-b")
            created = groups.create_group(
                repo_root=repo,
                group_id="pd-group",
                member_specs=["prefill=session-a", "decode=session-b"],
                startup_order=["decode", "prefill"],
                snapshot_resolver=same_snapshot,
                created_at=NOW,
            )
            self.assertEqual(created["shutdown_order"], ["prefill", "decode"])
            status = groups.inspect_group(
                repo_root=repo,
                group_id="pd-group",
                snapshot_resolver=same_snapshot,
            )
            self.assertEqual(status["status"], "ready")
            self.assertTrue(status["same_snapshot"])

    def test_teardown_uses_reverse_startup_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_session(repo, "session-a", "host-a")
            write_session(repo, "session-b", "host-b")
            groups.create_group(
                repo_root=repo,
                group_id="pd-group",
                member_specs=["prefill=session-a", "decode=session-b"],
                startup_order=["prefill", "decode"],
                snapshot_resolver=same_snapshot,
                created_at=NOW,
            )
            calls: list[str] = []

            def runner(command, **_kwargs):
                calls.append(command[command.index("--session-id") + 1])
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout='{"status":"removed"}',
                    stderr="",
                )

            result = groups.teardown_group(
                repo_root=repo,
                group_id="pd-group",
                remove_containers=True,
                remove_worktrees=True,
                release_leases=True,
                force=True,
                runner=runner,
                updated_at=NOW,
            )
            self.assertEqual(calls, ["session-b", "session-a"])
            self.assertEqual(result["status"], "removed")


if __name__ == "__main__":
    unittest.main()
