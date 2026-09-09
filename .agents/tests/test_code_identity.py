#!/usr/bin/env python3
"""Git-object code identity and parity-ref garbage collection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_coordinator.code_identity import (  # noqa: E402
    _load_parity,
    code_identity,
    collect_referenced_snapshot_commits,
    gc_parity_refs,
    manifest_code,
)
from vaws_coordinator.run_manifest import new_manifest, write_manifest  # noqa: E402

NOW = "2026-09-08T12:00:00Z"


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> str:
    _run(path, "init")
    _run(path, "config", "user.email", "identity@example.invalid")
    _run(path, "config", "user.name", "Identity Test")
    (path / "README").write_text("base\n", encoding="utf-8")
    _run(path, "add", "README")
    _run(path, "commit", "-m", "init")
    return _run(path, "rev-parse", "HEAD")


class CodeIdentityTests(unittest.TestCase):
    def test_dirty_tree_snapshot_fills_manifest_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_head = _init_repo(repo)
            (repo / "dirty.txt").write_text("changed\n", encoding="utf-8")
            identity = code_identity(repo)
            self.assertTrue(identity["dirty"])
            self.assertEqual(identity["source_head"], source_head)
            self.assertRegex(identity["snapshot_commit"], r"^[0-9a-f]{40}$")
            self.assertNotEqual(identity["snapshot_commit"], source_head)
            manifest = new_manifest(
                run_type="debug",
                created_at=NOW,
                code=manifest_code(repo),
            )
            self.assertEqual(manifest["code"]["source_head"], source_head)
            self.assertEqual(
                manifest["code"]["snapshot_commit"], identity["snapshot_commit"]
            )
            self.assertTrue(manifest["code"]["dirty"])
            self.assertRegex(manifest["code"]["snapshot_commit"], r"^[0-9a-f]{40}$")
            self.assertNotEqual(manifest["code"]["snapshot_commit"], source_head)

    def test_clean_tree_snapshot_equals_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source_head = _init_repo(repo)
            identity = code_identity(repo)
            self.assertFalse(identity["dirty"])
            self.assertEqual(identity["source_head"], source_head)
            self.assertEqual(identity["snapshot_commit"], source_head)
            manifest = new_manifest(
                run_type="debug",
                created_at=NOW,
                code=manifest_code(repo),
            )
            self.assertEqual(manifest["code"]["snapshot_commit"], source_head)
            self.assertFalse(manifest["code"]["dirty"])

    def test_dirty_snapshot_matches_parity_with_other_workspace_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            (repo / "dirty.txt").write_text("changed\n", encoding="utf-8")
            identity = code_identity(repo)
            parity = _load_parity()
            records = parity.build_snapshot_records(
                repo,
                "any-other-workspace-id",
                "sync-snapshot",
                tuple(parity.DEFAULT_DENYLIST),
                unpopulated="gitlink",
            )
            root = next(record for record in records if record.relpath == ".")
            self.assertEqual(identity["snapshot_commit"], root.commit)
            parity.cleanup_synthetic_refs(repo, records)

    def test_unpopulated_gitlink_is_the_submodule_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            child = repo / "vllm-ascend"
            child.mkdir()
            _init_repo(child)
            gitlink = "ab" * 20
            _run(
                child,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{gitlink},csrc/third_party/fake",
            )
            child_head = _run(child, "rev-parse", "HEAD")
            (repo / ".gitmodules").write_text(
                '[submodule "vllm-ascend"]\n\tpath = vllm-ascend\n\turl = ./vllm-ascend\n',
                encoding="utf-8",
            )
            _run(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{child_head},vllm-ascend",
            )
            _run(repo, "add", ".gitmodules")
            (repo / "dirty.txt").write_text("changed\n", encoding="utf-8")
            identity = code_identity(repo)
            self.assertTrue(identity["dirty"])
            self.assertEqual(
                identity["repos"]["vllm-ascend/csrc/third_party/fake"]["snapshot_commit"],
                gitlink,
            )


class ParityRefGcTests(unittest.TestCase):
    def test_gc_deletes_old_unref_and_keeps_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            first = _init_repo(repo)
            (repo / "second.txt").write_text("next\n", encoding="utf-8")
            _run(repo, "add", "second.txt")
            _run(repo, "commit", "-m", "second")
            second = _run(repo, "rev-parse", "HEAD")
            (repo / "third.txt").write_text("third\n", encoding="utf-8")
            _run(repo, "add", "third.txt")
            _run(repo, "commit", "-m", "third")
            third = _run(repo, "rev-parse", "HEAD")

            old_unref = "refs/parity/ws/old-unref/workspace"
            old_ref = "refs/parity/ws/old-kept/workspace"
            recent = "refs/parity/ws/recent/workspace"
            _run(repo, "update-ref", old_unref, first)
            _run(repo, "update-ref", old_ref, second)
            _run(repo, "update-ref", recent, third)

            git_dir = Path(_run(repo, "rev-parse", "--git-dir"))
            if not git_dir.is_absolute():
                git_dir = repo / git_dir
            stale = time.time() - 10 * 86400
            os.utime(git_dir / old_unref, (stale, stale))
            os.utime(git_dir / old_ref, (stale, stale))

            manifest_dir = repo / ".vaws-local" / "runs"
            manifest_dir.mkdir(parents=True)
            write_manifest(
                manifest_dir / "manifest.json",
                new_manifest(
                    run_type="debug",
                    created_at=NOW,
                    code={
                        "source_head": third,
                        "snapshot_commit": second,
                        "dirty": True,
                    },
                ),
            )
            self.assertEqual(
                collect_referenced_snapshot_commits(repo), {second}
            )

            result = gc_parity_refs(repo, max_age_days=7)
            listed = _run(
                repo,
                "for-each-ref",
                "--format=%(refname)",
                "refs/parity",
            ).splitlines()
            self.assertNotIn(old_unref, listed)
            self.assertIn(old_ref, listed)
            self.assertIn(recent, listed)
            deleted_refs = {row["ref"] for row in result["deleted"]}
            kept_refs = {row["ref"] for row in result["kept"]}
            self.assertIn(old_unref, deleted_refs)
            self.assertIn(old_ref, kept_refs)
            self.assertIn(recent, kept_refs)


if __name__ == "__main__":
    unittest.main()
