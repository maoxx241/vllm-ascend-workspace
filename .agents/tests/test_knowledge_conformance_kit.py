#!/usr/bin/env python3
"""Portable client run of the pinned vaws-knowledge shared kit."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "tests"))

from knowledge_kit import (  # noqa: E402
    EXPECTED_VECTOR_COUNT,
    KIT_ROOT_ENV,
    KitInvalid,
    KitUnconfigured,
    pinned_commit,
    resolve_kit_root,
    run_client_kit,
)

ADAPTER = ROOT / ".agents" / "tests" / "knowledge_client_adapter.py"


class KitConfigurationTests(unittest.TestCase):
    def test_unconfigured_reports_exact_skip_text(self) -> None:
        with self.assertRaises(KitUnconfigured) as caught:
            resolve_kit_root(repo_root=ROOT, environ={}, read_local_file=False)
        self.assertIn(f"{KIT_ROOT_ENV} is not set", str(caught.exception))
        self.assertIn(pinned_commit(ROOT), str(caught.exception))

    def test_runner_commands_quote_spaces(self) -> None:
        command = shlex.join(
            ["/opt/Python 3.12/python", "/tmp/dir with spaces/adapter.py", "hash"]
        )
        self.assertEqual(len(shlex.split(command)), 3)

    def test_configured_missing_path_fails(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "missing-kit"
        with self.assertRaises(KitInvalid) as caught:
            resolve_kit_root(
                repo_root=ROOT,
                environ={KIT_ROOT_ENV: str(missing)},
                read_local_file=False,
            )
        self.assertIn("does not exist", str(caught.exception))

    def test_configured_non_git_stub_is_rejected(self) -> None:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "conformance" / "vectors").mkdir(parents=True)
        (root / "conformance" / "runner.py").write_text("# synthetic non-kit\n", encoding="utf-8")
        for index in range(EXPECTED_VECTOR_COUNT):
            (root / "conformance" / "vectors" / f"stub-{index}.yaml").write_text(
                "id: synthetic-non-kit\n", encoding="utf-8"
            )
        try:
            with self.assertRaises(KitInvalid) as caught:
                resolve_kit_root(
                    repo_root=ROOT,
                    environ={KIT_ROOT_ENV: str(root)},
                    read_local_file=False,
                )
            self.assertIn("not a Git checkout", str(caught.exception))
        finally:
            temp.cleanup()

    def test_worktree_file_and_space_path_check_revision(self) -> None:
        temp = tempfile.TemporaryDirectory()
        repo = Path(temp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "kit-test@example.invalid"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "kit-test"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        (repo / "conformance" / "vectors").mkdir(parents=True)
        (repo / "conformance" / "runner.py").write_text("# stub\n", encoding="utf-8")
        for index in range(EXPECTED_VECTOR_COUNT):
            (repo / "conformance" / "vectors" / f"vector-{index:02d}.yaml").write_text(
                "id: stub\n", encoding="utf-8"
            )
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "stub"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        worktree = Path(temp.name) / "wt with spaces"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        try:
            self.assertTrue((worktree / ".git").is_file())
            with self.assertRaises(KitInvalid) as caught:
                resolve_kit_root(
                    repo_root=ROOT,
                    environ={KIT_ROOT_ENV: str(worktree)},
                    read_local_file=False,
                )
            self.assertIn(pinned_commit(ROOT), str(caught.exception))
        finally:
            temp.cleanup()

    def test_configured_wrong_revision_fails(self) -> None:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "kit-test@example.invalid"],
            cwd=str(root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "kit-test"],
            cwd=str(root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "wrong"],
            cwd=str(root),
            check=True,
            capture_output=True,
        )
        (root / "conformance" / "vectors").mkdir(parents=True)
        (root / "conformance" / "runner.py").write_text("# stub\n", encoding="utf-8")
        for index in range(EXPECTED_VECTOR_COUNT):
            (root / "conformance" / "vectors" / f"vector-{index:02d}.yaml").write_text(
                "id: stub\n", encoding="utf-8"
            )
        try:
            with self.assertRaises(KitInvalid) as caught:
                resolve_kit_root(
                    repo_root=ROOT,
                    environ={KIT_ROOT_ENV: str(root)},
                    read_local_file=False,
                )
            self.assertIn("expected", str(caught.exception))
            self.assertIn(pinned_commit(ROOT), str(caught.exception))
        finally:
            temp.cleanup()


class ConfiguredSharedKitTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.kit = resolve_kit_root(repo_root=ROOT, read_local_file=False)
        except KitUnconfigured as exc:
            self.skipTest(str(exc))

    def test_configured_31_vectors_including_export(self) -> None:
        completed = run_client_kit(self.kit, repo_root=ROOT)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS  export-idempotent-unchanged-entry", completed.stdout)
        self.assertIn("PASS  export-idempotent-scrambled-key-order", completed.stdout)
        self.assertIn("PASS  redaction-email", completed.stdout)
        self.assertIn("31 passed, 0 failed, 0 skipped, 31 total", completed.stdout)
        self.assertIn("conformance PASSED", completed.stdout)

    def test_portable_git_worktree_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "portable worktree with spaces"
            added = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.kit),
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    pinned_commit(ROOT),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
            try:
                self.assertTrue((worktree / ".git").is_file())
                resolved = resolve_kit_root(
                    repo_root=ROOT,
                    environ={KIT_ROOT_ENV: str(worktree)},
                    read_local_file=False,
                )
                self.assertEqual(resolved, worktree.resolve())
                completed = run_client_kit(resolved, repo_root=ROOT)
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                self.assertIn("31 passed, 0 failed, 0 skipped, 31 total", completed.stdout)
            finally:
                subprocess.run(
                    ["git", "-C", str(self.kit), "worktree", "remove", "--force", str(worktree)],
                    check=False,
                    capture_output=True,
                    text=True,
                )

    def test_dirty_executed_runner_is_rejected(self) -> None:
        runner = self.kit / "conformance" / "runner.py"
        original = runner.read_bytes()
        runner.write_bytes(original + b"\n# synthetic dirty-byte probe\n")
        try:
            with self.assertRaises(KitInvalid) as caught:
                resolve_kit_root(
                    repo_root=ROOT,
                    environ={KIT_ROOT_ENV: str(self.kit)},
                    read_local_file=False,
                )
            self.assertIn("do not match", str(caught.exception))
        finally:
            runner.write_bytes(original)

    def test_dirty_executed_vector_is_rejected(self) -> None:
        vector = next((self.kit / "conformance" / "vectors").glob("*.yaml"))
        original = vector.read_bytes()
        vector.write_bytes(original + b"\n# synthetic dirty-byte probe\n")
        try:
            with self.assertRaises(KitInvalid) as caught:
                resolve_kit_root(
                    repo_root=ROOT,
                    environ={KIT_ROOT_ENV: str(self.kit)},
                    read_local_file=False,
                )
            self.assertIn("do not match", str(caught.exception))
        finally:
            vector.write_bytes(original)

    def test_redaction_email_vector_through_tracked_adapter(self) -> None:
        import yaml  # noqa: PLC0415

        vector = yaml.safe_load(
            (self.kit / "conformance" / "gate_vectors" / "redaction-email.yaml").read_text(
                encoding="utf-8"
            )
        )
        completed = subprocess.run(
            [sys.executable, str(ADAPTER), "redaction"],
            check=False,
            capture_output=True,
            text=True,
            input=json.dumps(vector["document"]),
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), "reject")
        self.assertEqual(vector["expected_verdict"], "reject")

    def test_adapter_crash_is_not_a_reject_token(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ADAPTER), "schema"],
            check=False,
            capture_output=True,
            text=True,
            input="{",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("reject", completed.stdout.strip().splitlines()[-1:] or [""])


if __name__ == "__main__":
    unittest.main()
