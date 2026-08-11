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


def write_session(repo: Path, session_id: str, machine: str) -> None:
    path = repo / ".vaws-local" / "sessions" / session_id / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "base_machine": machine,
        "status": "ready",
        "local": {"worktree_root": str(repo / "worktrees" / session_id)},
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
