#!/usr/bin/env python3
"""Tests for the lightweight SessionEnd knowledge hook."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
HOOK = ROOT / ".agents" / "hooks" / "knowledge_session_end.py"
CODEX_EXAMPLE = ROOT / ".codex" / "config.example.toml"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_knowledge import (  # noqa: E402
    KNOWLEDGE_FILES,
    capture_candidate,
    knowledge_session_key,
)

NOW = "2026-07-27T12:00:00Z"


def load_hook():
    name = "_knowledge_session_end_test"
    spec = importlib.util.spec_from_file_location(name, HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hook = load_hook()


def write_knowledge(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for filename, kind in KNOWLEDGE_FILES.items():
        (root / filename).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": kind,
                    "updated_at": "2026-07-27",
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )


def candidate_payload(session_id: str) -> dict:
    return {
        "kind": "known-failure-signatures",
        "summary": "Explicit master endpoint avoids DNS delay",
        "owner_skill": "vllm-ascend-distributed-debug",
        "scope": {"component": ["torchrun"], "machine": ["hvv-sz"]},
        "fingerprints": ["torchrun standalone dns resolution delay"],
        "symptom": "A two-rank smoke spends tens of seconds resolving hostnames.",
        "root_cause": "Standalone rendezvous performs slow DNS resolution.",
        "resolution": "Use an explicit loopback master address and leased port.",
        "avoidance": "Prefer explicit rendezvous endpoints in deterministic smokes.",
        "applicable_versions": "workspace-managed hvv-sz validation",
        "verification": {
            "status": "passed",
            "checks": ["Two-rank all-reduce completed without DNS warnings."],
        },
        "evidence": [
            {
                "kind": "commit",
                "uri": "commit:c015161",
                "stable": True,
            }
        ],
        "confidence": "medium",
        "source": {
            "session_id": session_id,
            "run_ids": ["distributed-case-explicit"],
            "commits": ["c015161"],
        },
    }


class SessionEndHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_knowledge(self.root / ".agents" / "knowledge")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def defer(self, session_id: str) -> str:
        session_key = knowledge_session_key(session_id)
        result = capture_candidate(
            candidate_payload(session_id),
            candidate_dir=(
                self.root
                / ".vaws-local"
                / "knowledge"
                / "pending"
                / session_key
            ),
            knowledge_dir=self.root / ".agents" / "knowledge",
            now=NOW,
        )
        return result["candidate_id"]

    def payload(self, session_id: str) -> dict:
        return {
            "session_id": session_id,
            "transcript_path": "/path/that/does/not/exist.jsonl",
            "cwd": str(self.root),
            "hook_event_name": "SessionEnd",
            "reason": "other",
        }

    def test_no_pending_session_has_zero_artifacts(self) -> None:
        result = hook.process_session_end(
            self.payload("empty-session"), repo_root=self.root
        )
        self.assertIsNone(result)
        self.assertFalse(
            (self.root / ".vaws-local" / "knowledge" / "session-end").exists()
        )

    def test_matching_pending_candidate_is_flushed_without_transcript(self) -> None:
        session_id = "thread-real"
        candidate_id = self.defer(session_id)
        formal_before = {
            path.name: path.read_bytes()
            for path in (self.root / ".agents" / "knowledge").glob("*.yaml")
        }
        result = hook.process_session_end(
            self.payload(session_id), repo_root=self.root
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["processed"][0]["candidate_id"], candidate_id)
        self.assertTrue(
            (
                self.root
                / ".vaws-local"
                / "knowledge"
                / "candidates"
                / f"{candidate_id}.json"
            ).is_file()
        )
        self.assertFalse(
            (
                self.root
                / ".vaws-local"
                / "knowledge"
                / "pending"
                / knowledge_session_key(session_id)
            ).exists()
        )
        formal_after = {
            path.name: path.read_bytes()
            for path in (self.root / ".agents" / "knowledge").glob("*.yaml")
        }
        self.assertEqual(formal_after, formal_before)

    def test_symlink_and_oversize_pending_files_are_not_flushed(self) -> None:
        session_id = "thread-invalid"
        session_key = knowledge_session_key(session_id)
        pending = (
            self.root
            / ".vaws-local"
            / "knowledge"
            / "pending"
            / session_key
        )
        pending.mkdir(parents=True)
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(candidate_payload(session_id)), encoding="utf-8")
        (pending / "linked.json").symlink_to(outside)
        (pending / "oversize.json").write_bytes(
            b"{" + b"x" * (hook.MAX_CANDIDATE_BYTES + 1) + b"}"
        )

        result = hook.process_session_end(
            self.payload(session_id), repo_root=self.root
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue((pending / "linked.json").is_symlink())
        self.assertTrue((pending / "oversize.json").is_file())
        candidate_dir = self.root / ".vaws-local" / "knowledge" / "candidates"
        self.assertFalse(candidate_dir.exists())

    def test_repeated_finalization_is_idempotent(self) -> None:
        session_id = "thread-repeat"
        candidate_id = self.defer(session_id)
        first = hook.process_session_end(
            self.payload(session_id), repo_root=self.root
        )
        second = hook.process_session_end(
            self.payload(session_id), repo_root=self.root
        )
        self.assertEqual(first["processed"][0]["candidate_id"], candidate_id)
        self.assertIsNone(second)
        candidate_path = (
            self.root
            / ".vaws-local"
            / "knowledge"
            / "candidates"
            / f"{candidate_id}.json"
        )
        self.assertEqual(
            json.loads(candidate_path.read_text(encoding="utf-8"))["occurrence_count"],
            1,
        )

    def test_other_session_pending_candidate_is_untouched(self) -> None:
        other = "other-thread"
        candidate_id = self.defer(other)
        result = hook.process_session_end(
            self.payload("current-thread"), repo_root=self.root
        )
        self.assertIsNone(result)
        self.assertTrue(
            (
                self.root
                / ".vaws-local"
                / "knowledge"
                / "pending"
                / knowledge_session_key(other)
                / f"{candidate_id}.json"
            ).is_file()
        )

    def test_cli_emits_no_stdout_or_stderr(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(
                {
                    "session_id": "no-pending",
                    "transcript_path": None,
                    "cwd": str(self.root),
                    "hook_event_name": "SessionEnd",
                    "reason": "other",
                }
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_malformed_cli_input_never_blocks_shutdown(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            input="{bad-json",
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_codex_example_registers_bounded_session_end_hook(self) -> None:
        config = tomllib.loads(CODEX_EXAMPLE.read_text(encoding="utf-8"))
        self.assertTrue(config["features"]["hooks"])
        session_end = config["hooks"]["SessionEnd"]
        self.assertEqual(len(session_end), 1)
        command_hook = session_end[0]["hooks"][0]
        self.assertEqual(command_hook["type"], "command")
        self.assertIn(".agents/hooks/knowledge_session_end.py", command_hook["command"])
        self.assertLessEqual(command_hook["timeout"], 3)
        self.assertTrue(HOOK.is_file())


if __name__ == "__main__":
    unittest.main()
