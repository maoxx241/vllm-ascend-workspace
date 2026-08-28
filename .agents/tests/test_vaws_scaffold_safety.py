#!/usr/bin/env python3
"""Local safety regression tests for VAWS scaffold helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import vaws_remote_toolbox as toolbox  # noqa: E402
from vaws_remote_toolbox import RemoteTarget, SshEndpoint  # noqa: E402
from vaws_session_id import normalize_session_id  # noqa: E402
from vaws_session_state import (  # noqa: E402
    SessionStateError,
    allocate_service_port,
    allocate_session_leases,
    release_service_port,
    session_live_leases,
    require_session_npu_lease,
    release_all_session_leases,
)
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


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
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

    def test_duplicate_job_id_is_blocked_before_remote_launch(self) -> None:
        original_dir = toolbox.JOB_STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                toolbox.JOB_STATE_DIR = Path(tmp)  # type: ignore[assignment]
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
                    leased_devices=[],
                )
                toolbox._save_job_record("job-collision", {"job_id": "job-collision", "target": target.to_dict()})
                payload = toolbox.start_remote_job(target, command="echo should-not-run", job_id="job-collision")
                self.assertEqual(payload["status"], "blocked")
                self.assertIn("already exists", payload["error"])
        finally:
            toolbox.JOB_STATE_DIR = original_dir  # type: ignore[assignment]

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
    def test_npu_execution_rejects_empty_and_stale_session_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = {"base_machine": "host", "session_id": "task", "leases": {"npu_devices": []}}
            with self.assertRaisesRegex(SessionStateError, "nonempty lease"):
                require_session_npu_lease(session, repo_root=root)
            allocate_session_leases(repo_root=root, machine_alias="host", session_id="task", requested_devices=[0], available_devices=[0, 1])
            with self.assertRaisesRegex(SessionStateError, "differs"):
                require_session_npu_lease(session, repo_root=root)
            session["leases"]["npu_devices"] = [0]
            self.assertEqual(require_session_npu_lease(session, repo_root=root), [0])
            release_all_session_leases(repo_root=root, session_id="task")
            with self.assertRaisesRegex(SessionStateError, "nonempty lease"):
                require_session_npu_lease(session, repo_root=root)

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

    def test_live_leases_include_allocated_service_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allocate_session_leases(
                repo_root=root,
                machine_alias="machine-a",
                session_id="sess-abc",
                requested_devices=[1],
                available_devices=[0, 1],
                container_ssh_port=46001,
                port_available=lambda _port: True,
            )
            allocate_service_port(
                repo_root=root,
                machine_alias="machine-a",
                session_id="sess-abc",
                requested_port=30001,
                port_available=lambda _port: True,
            )
            self.assertEqual(
                session_live_leases(
                    repo_root=root,
                    machine_alias="machine-a",
                    session_id="sess-abc",
                ),
                {
                    "npu_devices": [1],
                    "container_ssh_ports": [46001],
                    "service_ports": [30001],
                },
            )
            release_service_port(
                repo_root=root,
                machine_alias="machine-a",
                session_id="sess-abc",
                port=30001,
            )
            self.assertEqual(
                session_live_leases(
                    repo_root=root,
                    machine_alias="machine-a",
                    session_id="sess-abc",
                )["service_ports"],
                [],
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
                if args[:2] == ["submodule", "foreach"]:
                    events.append("submodule-branches")
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

            self.assertEqual(events, ["submodule-branches"])
            self.assertEqual(reused_root, worktree_root.resolve())
            self.assertEqual(reused_payload["action"], "reused")
            binding = json.loads((worktree_root / ".vaws-local" / "current-session.json").read_text())
            self.assertEqual(binding["session_id"], "sess-abc")
            self.assertEqual(binding["source"], "session_create-staging")

    def test_reuse_preserves_child_commit_branch_and_dirty_source(self) -> None:
        module = load_session_create_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "upstream"
            worktree = root / "worktree"
            child.mkdir()
            worktree.mkdir()

            def git(path, *args):
                return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()

            for repo in (child, worktree):
                git(repo, "init")
                git(repo, "config", "user.email", "test@example.invalid")
                git(repo, "config", "user.name", "Test")
            (child / "code.py").write_text("original\n")
            git(child, "add", ".")
            git(child, "commit", "-m", "base")
            git(worktree, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "vllm")
            git(worktree, "commit", "-am", "pin old child")
            sub = worktree / "vllm"
            git(sub, "config", "user.email", "test@example.invalid")
            git(sub, "config", "user.name", "Test")
            git(sub, "checkout", "-b", "developer-work")
            (sub / "code.py").write_text("committed change\n")
            git(sub, "commit", "-am", "keep me")
            head = git(sub, "rev-parse", "HEAD")
            (sub / "code.py").write_text("uncommitted change\n")
            binding = worktree / ".vaws-local" / "current-session.json"
            binding.parent.mkdir()
            binding.write_text(json.dumps({"session_id": "task"}))
            module.ensure_worktree(session_id="task", branch="session/task", base_ref="HEAD", worktree_root=worktree, no_worktree=False)
            self.assertEqual(git(sub, "rev-parse", "HEAD"), head)
            self.assertEqual(git(sub, "branch", "--show-current"), "developer-work")
            self.assertEqual((sub / "code.py").read_text(), "uncommitted change\n")


class RunStateIsolationTests(unittest.TestCase):
    def test_memory_profiling_run_dirs_are_unique_and_sanitized(self) -> None:
        module = load_script_module(
            "_vaws_mem_common_test",
            ROOT / ".agents" / "skills" / "ascend-memory-profiling" / "scripts" / "_common.py",
        )
        original_state_dir = module.MEMPROF_STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                module.MEMPROF_STATE_DIR = Path(tmp) / "memory"
                first = module.ensure_run_dir(tag="../same tag")
                second = module.ensure_run_dir(tag="../same tag")
                self.assertNotEqual(first, second)
                self.assertEqual(first.parent, module.MEMPROF_STATE_DIR)
                self.assertEqual(second.parent, module.MEMPROF_STATE_DIR)
                self.assertNotIn("..", first.name)
                self.assertNotIn("/", first.name)
        finally:
            module.MEMPROF_STATE_DIR = original_state_dir

    def test_profiling_collection_run_dirs_include_session_and_do_not_collide(self) -> None:
        module = load_script_module(
            "_vaws_profile_collection_common_test",
            ROOT / ".agents" / "skills" / "ascend-profiling-collection" / "scripts" / "_common.py",
        )
        original_state_dir = module.COLLECTION_STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                module.COLLECTION_STATE_DIR = Path(tmp) / "profile-runs"
                first = module.unique_collection_run_dir(tag="../same tag", session_id="sess-a")
                second = module.unique_collection_run_dir(tag="../same tag", session_id="sess-a")
                self.assertNotEqual(first, second)
                self.assertEqual(first.parent, module.COLLECTION_STATE_DIR)
                self.assertEqual(second.parent, module.COLLECTION_STATE_DIR)
                self.assertIn("sess-a", first.name)
                self.assertNotIn("..", first.name)
                self.assertNotIn("/", first.name)
        finally:
            module.COLLECTION_STATE_DIR = original_state_dir

    def test_benchmark_results_are_written_under_session_state(self) -> None:
        module = load_script_module(
            "_vaws_benchmark_common_test",
            ROOT / ".agents" / "skills" / "vllm-ascend-benchmark" / "scripts" / "_common.py",
        )
        original_root = module.ROOT
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                module.ROOT = root
                session_dir = root / ".vaws-local" / "sessions" / "sess-a"
                session_dir.mkdir(parents=True)
                session_file = session_dir / "session.json"
                session_file.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "session_id": "sess-a",
                            "base_machine": "machine-a",
                            "local": {"worktree_root": str(root)},
                            "remote": {"host": "173.131.1.2", "container": {"name": "c", "ssh_port": 46001}},
                        }
                    ),
                    encoding="utf-8",
                )
                config = module.BenchConfig(
                    session_id="sess-a",
                    session_file=str(session_file),
                    model="/models/Qwen",
                )
                payload = {"status": "ok"}
                result_path = module.write_local_result(config, payload)
                expected_parent = (
                    root / ".vaws-local" / "sessions" / "sess-a" / "benchmark" / "runs"
                )
                self.assertEqual(result_path.parent, expected_parent)
                saved = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "ok")
                self.assertEqual(saved["result_path"], str(result_path))
                self.assertEqual(saved["run_dir"], str(expected_parent))
        finally:
            module.ROOT = original_root


if __name__ == "__main__":
    unittest.main()
