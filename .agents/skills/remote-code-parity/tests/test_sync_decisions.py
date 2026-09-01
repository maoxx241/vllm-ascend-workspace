#!/usr/bin/env python3
"""Tests for the sync snapshot fast path and the auto apply-mode tier decision."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "remote-code-parity" / "scripts"
LIB = ROOT / ".agents" / "lib"
for path in (SCRIPTS, LIB):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


common = load_module("common", SCRIPTS / "common.py")
parity = load_module("_remote_code_parity_sync_decision_test", SCRIPTS / "remote_code_parity.py")

RUNTIME_ROOT = "/vllm-workspace"
MARKER_DIRNAME = ".vaws-runtime"
MARKER_PATH = parity.marker_path_for(RUNTIME_ROOT, MARKER_DIRNAME)
COMMITS = {"vllm": "c-vllm", "vllm-ascend": "c-ascend"}
INPUTS = {"native": "n1", "dependencies": "d1"}
MATCHING_MARKER = json.dumps({"container_identity": "container-a", "runtime_root": RUNTIME_ROOT})


def snapshot_record(relpath: str) -> parity.SnapshotRecord:
    return parity.SnapshotRecord(
        relpath=relpath,
        repo_id=relpath,
        source_head=COMMITS[relpath],
        parent=COMMITS[relpath],
        commit=COMMITS[relpath],
        tree="tree",
        ref=f"refs/parity/test/snapshot/{relpath}",
        changed_paths=[],
        submodules=[],
        build_inputs=dict(INPUTS),
    )


def runtime_state(
    *,
    last_commits: dict[str, str] | None = None,
    first_reinstall_completed: bool = True,
) -> dict:
    container = {
        "last_snapshot_commits": dict(COMMITS if last_commits is None else last_commits),
        "installed_build_inputs": {"vllm": dict(INPUTS), "vllm-ascend": dict(INPUTS)},
        "first_reinstall_completed": first_reinstall_completed,
        "last_runtime_install_env": {},
    }
    return {"servers": {"server-a": {"containers": {"container-a": container}}}}


def sync_args(workspace_root: Path, **overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        workspace_root=str(workspace_root),
        workspace_id="ws",
        server_name="server-a",
        runtime_root=RUNTIME_ROOT,
        container_identity="container-a",
        container_cache_root="/cache",
        marker_dirname=MARKER_DIRNAME,
        preserve_path=[],
        snapshot_id="snap-1",
        container_host="192.0.2.10",
        container_port=46001,
        container_user="root",
        source=[],
        apply_mode="auto",
        dry_run=False,
        force_reinstall=False,
        print_manifest=False,
        transport="auto",
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def ssh_dispatcher(marker_stdout: str, observed: dict[str, str]):
    """Serve the marker cat and the runtime commit probe; nothing else."""

    def fake(endpoint, script, **kwargs):
        if MARKER_PATH in script:
            return subprocess.CompletedProcess([], 0, marker_stdout, "")
        if "rev-parse HEAD" in script:
            out = "".join(f"{relpath} {commit}\n" for relpath, commit in observed.items())
            return subprocess.CompletedProcess([], 0, out, "")
        raise AssertionError(f"unexpected ssh_exec script: {script[:200]}")

    return fake


class SyncDecisionTests(unittest.TestCase):
    def run_sync(self, *, state: dict, marker_stdout: str, observed: dict[str, str]):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = sync_args(Path(tmp.name))
        tracked = {
            "ssh_exec": mock.Mock(side_effect=ssh_dispatcher(marker_stdout, observed)),
            "acquire_container_lock": mock.Mock(),
            "materialize_runtime": mock.Mock(),
        }
        patches = {
            "build_snapshot_records": mock.Mock(return_value=[snapshot_record("vllm"), snapshot_record("vllm-ascend")]),
            "load_runtime_state": mock.Mock(return_value=state),
            "repo_root_from": mock.Mock(return_value=Path(tmp.name)),
            "cleanup_synthetic_refs": mock.DEFAULT,
            "release_container_lock": mock.DEFAULT,
            "ensure_remote_bare_repos": mock.DEFAULT,
            "push_snapshot_to_mirror": mock.Mock(
                side_effect=lambda **kwargs: {"repo": kwargs["record"].relpath, "transport": "git"}
            ),
            "upload_manifest": mock.DEFAULT,
            **tracked,
        }
        stdout = io.StringIO()
        with mock.patch.multiple(parity, **patches), contextlib.redirect_stdout(stdout):
            returncode = parity.run_sync(args)
        return returncode, json.loads(stdout.getvalue()), tracked

    def test_snapshot_fast_path_short_circuits_install(self) -> None:
        returncode, summary, applied = self.run_sync(
            state=runtime_state(),
            marker_stdout=MATCHING_MARKER,
            observed=COMMITS,
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["fast_path"], "snapshot")
        self.assertEqual(summary["reinstall"], "not-needed")
        self.assertFalse(summary["first_install"])
        applied["acquire_container_lock"].assert_not_called()
        # Exactly the marker read plus the runtime commit probe, nothing more.
        self.assertEqual(applied["ssh_exec"].call_count, 2)

    def test_fast_path_requires_matching_snapshot_commits(self) -> None:
        returncode, summary, applied = self.run_sync(
            state=runtime_state(last_commits={"vllm": "stale", "vllm-ascend": "stale"}),
            marker_stdout="",
            observed=COMMITS,
        )

        self.assertNotEqual(summary.get("fast_path"), "snapshot")
        self.assertEqual(returncode, 2)
        self.assertEqual(summary["status"], "blocked")
        self.assertTrue(summary["first_install"])

    def test_fast_path_requires_first_reinstall_completed(self) -> None:
        returncode, summary, applied = self.run_sync(
            state=runtime_state(first_reinstall_completed=False),
            marker_stdout=MATCHING_MARKER,
            observed=COMMITS,
        )

        self.assertNotEqual(summary.get("fast_path"), "snapshot")
        # Auto mode then re-checks the marker and selects the materialize tier.
        self.assertEqual(returncode, 0)
        self.assertEqual(summary["status"], "materialized")
        self.assertEqual(summary["apply_mode"], "materialize")
        applied["materialize_runtime"].assert_called_once()

    def test_fast_path_requires_matching_marker_identity(self) -> None:
        foreign_marker = json.dumps({"container_identity": "other", "runtime_root": RUNTIME_ROOT})
        returncode, summary, applied = self.run_sync(
            state=runtime_state(),
            marker_stdout=foreign_marker,
            observed=COMMITS,
        )

        self.assertNotEqual(summary.get("fast_path"), "snapshot")
        self.assertEqual(returncode, 2)
        self.assertEqual(summary["status"], "blocked")
        self.assertTrue(summary["first_install"])

    def test_fast_path_requires_clean_runtime_commits(self) -> None:
        drifted = {"vllm": "dirty-runtime", "vllm-ascend": "c-ascend"}
        returncode, summary, applied = self.run_sync(
            state=runtime_state(),
            marker_stdout=MATCHING_MARKER,
            observed=drifted,
        )

        self.assertNotEqual(summary.get("fast_path"), "snapshot")
        # Auto mode selects materialize, which then fails commit verification.
        self.assertEqual(returncode, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["reason"], "runtime commit verification mismatch")
        applied["materialize_runtime"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
