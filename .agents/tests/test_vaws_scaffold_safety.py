#!/usr/bin/env python3
"""Local safety regression tests for VAWS scaffold helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import vaws_remote_toolbox as toolbox  # noqa: E402
from vaws_remote_toolbox import RemoteTarget, SshEndpoint  # noqa: E402
from vaws_session_id import normalize_session_id  # noqa: E402
from vaws_session_state import SessionStateError, allocate_session_leases  # noqa: E402
from vaws_validate import (  # noqa: E402
    ValidationError,
    parse_device_csv,
    require_env_name,
    require_safe_id,
)


def load_session_create_module():
    path = ROOT / ".agents" / "skills" / "session-management" / "scripts" / "session_create.py"
    spec = importlib.util.spec_from_file_location("_vaws_session_create_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ValidatorTests(unittest.TestCase):
    def test_safe_id_rejects_path_shapes(self) -> None:
        for value in ("../x", "a/b", "/tmp/x", "..", "a b", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    require_safe_id(value, label="job id")

    def test_env_name_is_ascii_shell_identifier(self) -> None:
        self.assertEqual(require_env_name("VLLM_USE_V1"), "VLLM_USE_V1")
        for value in ("A-B", "1ABC", "A;echo", "", "\u53d8\u91cf"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    require_env_name(value)

    def test_device_csv_rejects_invalid_inputs(self) -> None:
        self.assertEqual(parse_device_csv("2,0,1"), [0, 1, 2])
        for value in ("-1", "0,0", "999,,1", "", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_device_csv(value)


class SessionIdTests(unittest.TestCase):
    def test_long_session_ids_keep_hash_suffix(self) -> None:
        raw_a = "feature-" + ("a" * 80) + "-111"
        raw_b = "feature-" + ("a" * 80) + "-222"
        sid_a = normalize_session_id(raw_a)
        sid_b = normalize_session_id(raw_b)
        self.assertIsNotNone(sid_a)
        self.assertIsNotNone(sid_b)
        assert sid_a is not None and sid_b is not None
        self.assertLessEqual(len(sid_a), 64)
        self.assertLessEqual(len(sid_b), 64)
        self.assertNotEqual(sid_a, sid_b)


class RemoteToolboxSafetyTests(unittest.TestCase):
    def test_job_record_path_stays_under_job_state_dir(self) -> None:
        valid = toolbox._job_record_path("job-abc_123")
        self.assertIn(toolbox.JOB_STATE_DIR.resolve(), valid.parents)
        with self.assertRaises(ValidationError):
            toolbox._job_record_path("../sessions/leases")

    def test_env_items_validate_before_shell_export(self) -> None:
        self.assertEqual(toolbox._parse_env_items(["A_B=1"]), {"A_B": "1"})
        with self.assertRaises(ValidationError):
            toolbox._parse_env_items(["A-B=1"])

    def test_cleanup_leases_only_is_blocked_for_session_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = RemoteTarget(
                mode="session",
                alias="machine-a",
                target_id="sess-abc",
                workspace_id="sess-abc",
                workspace_root=Path(tmp),
                runtime_root="/workspace",
                container_name="vaws-test",
                container_image="image",
                container_endpoint=SshEndpoint("127.0.0.1", 46000),
                host_endpoint=SshEndpoint("127.0.0.1", 22),
                state_repo_root=Path(tmp),
                record={},
                session_id="sess-abc",
                session_file=Path(tmp) / "session.json",
                session={"session_id": "sess-abc"},
                leased_devices=[0],
            )
            payload = toolbox.cleanup(
                target,
                dry_run=True,
                jobs=False,
                job_ids=None,
                service=False,
                session_container=False,
                leases=True,
                known_hosts=False,
                remote_temp=False,
                force=False,
            )
            self.assertEqual(payload["status"], "blocked")

    def test_artifact_manifest_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.txt"
            target.write_text("secret\n", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(target)
            with self.assertRaises(toolbox.RemoteToolboxError):
                toolbox._local_manifest(link)


class LeaseValidationTests(unittest.TestCase):
    def test_session_lease_validates_devices_against_host_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SessionStateError):
                allocate_session_leases(
                    repo_root=root,
                    machine_alias="machine-a",
                    session_id="sess-abc",
                    requested_devices=[999],
                    available_devices=[0, 1],
                    port_available=lambda _port: True,
                )
            with self.assertRaises(SessionStateError):
                allocate_session_leases(
                    repo_root=root,
                    machine_alias="machine-a",
                    session_id="sess-abc",
                    npu_count=0,
                    port_available=lambda _port: True,
                )


class WorktreeCreateTests(unittest.TestCase):
    def test_staging_binding_is_written_before_submodule_update(self) -> None:
        module = load_session_create_module()
        original_run_git = module.run_git
        original_write_binding = module.write_current_session_binding
        original_emit_progress = module.emit_progress
        events: list[str] = []
        fail_submodule = True
        with tempfile.TemporaryDirectory() as tmp:
            worktree_root = Path(tmp) / "worktree"

            def fake_run_git(args, *, cwd=module.ROOT, check=True):
                if args[:3] == ["show-ref", "--verify", "--quiet"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                if args[:2] == ["worktree", "add"]:
                    worktree_root.mkdir(parents=True)
                    events.append("worktree-add")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if args == ["submodule", "update", "--init", "--recursive"]:
                    events.append("submodule-update")
                    if fail_submodule:
                        raise RuntimeError("submodule update failed")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected git args: {args!r}")

            def fake_write_binding(repo_root, *, session_id, source, **_kwargs):
                events.append("binding")
                path = Path(repo_root) / ".vaws-local" / "current-session.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"session_id": session_id, "source": source}) + "\n",
                    encoding="utf-8",
                )
                return path

            try:
                module.run_git = fake_run_git
                module.write_current_session_binding = fake_write_binding
                module.emit_progress = lambda *_args, **_kwargs: None
                with self.assertRaisesRegex(RuntimeError, "submodule update failed"):
                    module.ensure_worktree(
                        session_id="sess-abc",
                        branch="session/sess-abc",
                        base_ref="main",
                        worktree_root=worktree_root,
                        no_worktree=False,
                    )
                self.assertEqual(events, ["worktree-add", "binding", "submodule-update"])
                fail_submodule = False
                events.clear()
                reused_root, reused_payload = module.ensure_worktree(
                    session_id="sess-abc",
                    branch="session/sess-abc",
                    base_ref="main",
                    worktree_root=worktree_root,
                    no_worktree=False,
                )
            finally:
                module.run_git = original_run_git
                module.write_current_session_binding = original_write_binding
                module.emit_progress = original_emit_progress

            self.assertEqual(events, ["submodule-update"])
            self.assertEqual(reused_root, worktree_root.resolve())
            self.assertEqual(reused_payload["action"], "reused")
            binding = json.loads((worktree_root / ".vaws-local" / "current-session.json").read_text())
            self.assertEqual(binding["session_id"], "sess-abc")
            self.assertEqual(binding["source"], "session_create-staging")


if __name__ == "__main__":
    unittest.main()
