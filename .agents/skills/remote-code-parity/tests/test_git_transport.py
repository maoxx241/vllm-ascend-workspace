from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
import os
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
parity = load_module("_remote_code_parity_git_transport_test", SCRIPTS / "remote_code_parity.py")
wrapper = load_module("_parity_sync_git_transport_test", SCRIPTS / "parity_sync.py")


def snapshot_record(relpath: str = "vllm"):
    return parity.SnapshotRecord(
        relpath=relpath,
        repo_id="workspace" if relpath == "." else relpath,
        source_head="source",
        parent="source",
        commit="snapshot",
        tree="tree",
        ref=f"refs/parity/test/snapshot/{relpath}",
        changed_paths=[],
        submodules=[],
    )


class GitTransportTests(unittest.TestCase):
    def test_external_business_worktrees_snapshot_without_reset_or_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "scaffold"
            def git(repo, *args):
                return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
            def init(repo):
                repo.mkdir()
                git(repo, "init")
                git(repo, "config", "user.name", "Test")
                git(repo, "config", "user.email", "test@example.invalid")
                (repo / "model.py").write_text("original\n")
                git(repo, "add", ".")
                git(repo, "commit", "-m", "base")
            init(root)
            sources = {}
            module_lines = []
            for name in ("vllm", "vllm-ascend"):
                repo = base / ("actual-" + name)
                init(repo)
                sources[name] = repo
                module_lines.append(f'[submodule "{name}"]\n path = {name}\n url = {repo}\n')
                git(root, "update-index", "--add", "--cacheinfo", "160000," + git(repo, "rev-parse", "HEAD") + "," + name)
            (root / ".gitmodules").write_text("\n".join(module_lines))
            git(root, "add", ".gitmodules")
            git(root, "commit", "-m", "links")
            (sources["vllm-ascend"] / "model.py").write_text("actual dirty edit\n")
            before = {name: (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain")) for name, repo in sources.items()}
            records = parity.build_snapshot_records(root, "external", "snapshot", (), sources)
            record = next(row for row in records if row.relpath == "vllm-ascend")
            self.assertEqual(Path(record.source_path), sources["vllm-ascend"].resolve())
            self.assertEqual(git(sources["vllm-ascend"], "show", record.commit + ":model.py"), "actual dirty edit")
            self.assertFalse((root / "vllm-ascend").exists())
            self.assertEqual(before, {name: (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain")) for name, repo in sources.items()})
            parity.cleanup_synthetic_refs(root, records)

    def test_watcher_always_stages_and_acknowledges_only_pre_sync_content(self):
        watcher = load_module("_parity_watcher_test", SCRIPTS / "parity_watch.py")
        args = argparse.Namespace(apply_mode="auto", force_reinstall=False, dry_run=False,
                                  print_manifest=True, workspace_root="/tmp/scaffold", source=[])
        def sync(args):
            self.assertEqual(args.apply_mode, "source-only")
            print('{"status":"ready","snapshot_commits":{"vllm":"snapshot"}}')
            return 0
        with mock.patch.object(watcher.parity, "workspace_fingerprint", side_effect=["before", "during", "during"]), mock.patch.object(watcher.parity, "run_sync", side_effect=sync) as execute:
            fingerprint, result = watcher.stage_once(args, None)
            self.assertIsNotNone(result)
            fingerprint, result = watcher.stage_once(args, fingerprint)
            self.assertIsNotNone(result)
            _, result = watcher.stage_once(args, fingerprint)
            self.assertIsNone(result)
            self.assertEqual(execute.call_count, 2)

    def test_dirty_file_content_and_untracked_edits_change_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
            git("init")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            path = root / "model.py"
            path.write_text("base\n")
            git("add", ".")
            git("commit", "-m", "base")
            path.write_text("edit one\n")
            first = parity.workspace_fingerprint(root)
            path.write_text("edit two\n")
            second = parity.workspace_fingerprint(root)
            self.assertNotEqual(first, second)
            extra = root / "new.py"
            extra.write_text("one\n")
            third = parity.workspace_fingerprint(root)
            extra.write_text("two\n")
            self.assertNotEqual(third, parity.workspace_fingerprint(root))

    def test_native_inputs_reuse_python_commits_and_detect_reverts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
            git("init")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (root / "model.py").write_text("base\n")
            (root / "kernel.cpp").write_text("native one\n")
            (root / "requirements.txt").write_text("dependency==1\n")
            git("add", ".")
            git("commit", "-m", "base")
            def inputs():
                return parity.build_input_fingerprints(root, "HEAD", parity.VLLM_REINSTALL_PATTERNS)
            first = inputs()
            (root / "model.py").write_text("python edit\n")
            git("commit", "-am", "python only")
            self.assertEqual(parity.changed_build_inputs(inputs(), first), (False, False))
            (root / "kernel.cpp").write_text("native two\n")
            git("commit", "-am", "native edit")
            native = inputs()
            self.assertEqual(parity.changed_build_inputs(native, first), (True, False))
            git("revert", "--no-edit", "HEAD")
            self.assertEqual(parity.changed_build_inputs(inputs(), native), (True, False))
            self.assertEqual(inputs(), first)
            with mock.patch.dict(os.environ, {"VAWS_ENVIRONMENT_FINGERPRINT": "different-profile"}):
                self.assertEqual(parity.changed_build_inputs(inputs(), first), (True, False))
            self.assertEqual(parity.changed_build_inputs(first, None), (True, True))

    def test_wrapper_forwards_default_transport(self) -> None:
        derived = {
            "workspace_root": "/worktree",
            "workspace_id": "test",
            "server_name": "server-a",
            "runtime_root": "/vllm-workspace",
            "container_identity": "container@/vllm-workspace",
            "container_cache_root": "/cache",
            "container_host": "host-a",
            "container_port": 46001,
            "container_user": "root",
            "preserve_path": [],
        }
        args = argparse.Namespace(
            snapshot_id=None,
            print_manifest=False,
            force_reinstall=False,
            dry_run=False,
            apply_mode="materialize",
        )

        command = wrapper.build_low_level_command(derived, args)

        index = command.index("--transport")
        self.assertEqual(command[index + 1], "auto")

    def test_git_remote_url_supports_ipv4_ipv6_and_escaped_paths(self) -> None:
        ipv4 = common.SshEndpoint(host="10.0.0.2", port=46001, user="root")
        ipv6 = common.SshEndpoint(host="2001:db8::2", port=22, user="build user")

        self.assertEqual(
            parity.git_remote_url(ipv4, "/cache/workspace.git"),
            "ssh://root@10.0.0.2:46001/cache/workspace.git",
        )
        self.assertEqual(
            parity.git_remote_url(ipv6, "/cache/a repo.git"),
            "ssh://build%20user@[2001:db8::2]:22/cache/a%20repo.git",
        )

    def test_git_transport_is_noninteractive_and_uses_endpoint_port(self) -> None:
        endpoint = common.SshEndpoint(host="host", port=46001, user="root")

        env = parity.git_ssh_environment(endpoint)

        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])
        self.assertIn("46001", env["GIT_SSH_COMMAND"])

    def test_transport_carrier_ref_is_stable_and_target_scoped(self) -> None:
        first = common.SshEndpoint(host="host-a", port=22, user="root")
        second = common.SshEndpoint(host="host-b", port=22, user="root")
        record = snapshot_record()

        first_ref = parity.transport_carrier_ref(first, "/cache/vllm.git", "workspace", record)
        repeated_ref = parity.transport_carrier_ref(first, "/cache/vllm.git", "workspace", record)
        second_ref = parity.transport_carrier_ref(second, "/cache/vllm.git", "workspace", record)

        self.assertEqual(first_ref, repeated_ref)
        self.assertNotEqual(first_ref, second_ref)
        self.assertTrue(first_ref.startswith("refs/parity-transport/workspace/"))

    def test_git_push_publishes_snapshot_and_transport_carrier(self) -> None:
        endpoint = common.SshEndpoint(host="host", port=22, user="root")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")

        with (
            mock.patch.object(parity, "remote_ref_commit", return_value=None),
            mock.patch.object(
                parity,
                "build_transport_carrier",
                return_value=("refs/parity-transport/local", "carrier-commit"),
            ),
            mock.patch.object(parity, "git_remote_url", return_value="ssh://remote/mirror.git"),
            mock.patch.object(parity, "git_ssh_environment", return_value={}) as environment,
            mock.patch.object(parity, "git", return_value=completed) as execute,
        ):
            result = parity.push_snapshot_via_git(
                Path("/repo"),
                container=endpoint,
                mirror_path="/cache/vllm.git",
                record=snapshot_record(),
                workspace_id="test",
            )

        command = execute.call_args.args[1]
        self.assertIn(
            "refs/parity-transport/local:refs/parity/test/transport-carrier",
            command,
        )
        self.assertIn(
            "refs/parity/test/snapshot/vllm:refs/parity/test/current",
            command,
        )
        self.assertEqual(result["carrier_commit"], "carrier-commit")
        environment.assert_called_once_with(endpoint)

    def test_auto_transport_prefers_git_without_creating_bundle(self) -> None:
        endpoint = common.SshEndpoint(host="host", port=22, user="root")
        expected = {"repo": "vllm", "transport": "git"}

        with (
            mock.patch.object(parity, "push_snapshot_via_git", return_value=expected) as push_git,
            mock.patch.object(parity, "push_snapshot_via_bundle") as push_bundle,
        ):
            result = parity.push_snapshot_to_mirror(
                Path("/repo"),
                container=endpoint,
                mirror_path="/cache/vllm.git",
                container_cache_root="/cache",
                record=snapshot_record(),
                workspace_id="test",
                dry_run=False,
                transport="auto",
            )

        self.assertEqual(result, expected)
        push_git.assert_called_once()
        push_bundle.assert_not_called()

    def test_auto_transport_falls_back_to_bundle(self) -> None:
        endpoint = common.SshEndpoint(host="host", port=22, user="root")

        with (
            mock.patch.object(parity, "push_snapshot_via_git", side_effect=RuntimeError("receive-pack denied")),
            mock.patch.object(
                parity,
                "push_snapshot_via_bundle",
                return_value={"repo": "vllm", "transport": "bundle"},
            ) as push_bundle,
            mock.patch.object(parity, "emit_progress") as progress,
        ):
            result = parity.push_snapshot_to_mirror(
                Path("/repo"),
                container=endpoint,
                mirror_path="/cache/vllm.git",
                container_cache_root="/cache",
                record=snapshot_record(),
                workspace_id="test",
                dry_run=False,
                transport="auto",
            )

        self.assertEqual(result["transport"], "bundle")
        self.assertEqual(result["fallback_from"], "git")
        push_bundle.assert_called_once()
        progress.assert_called_once()

    def test_forced_git_transport_does_not_fallback(self) -> None:
        endpoint = common.SshEndpoint(host="host", port=22, user="root")

        with (
            mock.patch.object(parity, "push_snapshot_via_git", side_effect=RuntimeError("denied")),
            mock.patch.object(parity, "push_snapshot_via_bundle") as push_bundle,
        ):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                parity.push_snapshot_to_mirror(
                    Path("/repo"),
                    container=endpoint,
                    mirror_path="/cache/workspace.git",
                    container_cache_root="/cache",
                    record=snapshot_record("."),
                    workspace_id="test",
                    dry_run=False,
                    transport="git",
                )

        push_bundle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
