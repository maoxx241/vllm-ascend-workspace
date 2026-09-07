from __future__ import annotations

import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manage_monitor.py"
SPEC = importlib.util.spec_from_file_location("npu_fleet_monitor_manager", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CANONICAL_HTTPS = "https://github.com/vllm-ascend-workspace/vaws-top.git"
CANONICAL_SSH = "git@github.com:vllm-ascend-workspace/vaws-top.git"
CANONICAL_SSH_URI = "ssh://git@github.com/vllm-ascend-workspace/vaws-top.git"


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def write_required_files(root: Path) -> None:
    for relative in MODULE.REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder\n", encoding="utf-8")
    (root / ".gitignore").write_text("data/\n.env\n", encoding="utf-8")


def init_canonical_repo(root: Path, origin: str = CANONICAL_HTTPS) -> str:
    root.mkdir(parents=True)
    write_required_files(root)
    git(root, "init")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    git(root, "remote", "add", "origin", origin)
    return git(root, "rev-parse", "HEAD").stdout.strip()


def write_pin(path: Path, commit: str, url: str = CANONICAL_HTTPS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": MODULE.CANONICAL_REPO.rsplit("/", 1)[-1],
                "repository": "vllm-ascend-workspace/vaws-top",
                "url": url,
                "ref": "main",
                "commit": commit,
            }
        ),
        encoding="utf-8",
    )


class ManageMonitorTests(unittest.TestCase):
    def test_default_endpoint_is_loopback(self) -> None:
        self.assertTrue(MODULE.DEFAULT_URL.startswith("http://127.0.0.1:"))
        self.assertTrue(MODULE.DASHBOARD_URL.startswith("http://127.0.0.1:"))

    def test_payload_exposes_project_local_agent_skill(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            worktree = Path(root)
            with mock.patch.object(MODULE, "health", return_value=(True, {}, None)), mock.patch.object(
                MODULE, "systemd_properties", return_value={}
            ):
                payload = MODULE.payload_for("status", worktree, "abc", None)
        self.assertEqual(payload["agent_skill"], str(worktree / ".agents/skills/vaws-top/SKILL.md"))
        self.assertEqual(payload["cli"], str(worktree / "scripts/vaws-top.py"))
        self.assertEqual(payload["mcp"], str(worktree / "scripts/vaws-top-mcp.py"))
        self.assertEqual(payload["start"], str(worktree / "scripts/start.sh"))
        self.assertEqual(payload["install_user_service"], str(worktree / "scripts/install-user-service.sh"))
        self.assertFalse(payload["allocation_authority"])

    def test_validate_project_rejects_missing_contract(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            pin = {"commit": "a" * 40, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
            with self.assertRaisesRegex(MODULE.MonitorError, "missing required files"):
                MODULE.validate_existing_checkout(Path(root), pin, require_clean_for_ensure=False)

    def test_source_does_not_use_old_branch_worktree_bootstrap(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("DEFAULT_BRANCH", text)
        self.assertNotIn("git worktree", text)
        self.assertNotIn("worktree add", text)
        self.assertNotIn("ensure_local_branch", text)
        self.assertNotIn("NFM_SOURCE_WORKSPACE", text)


class OriginIdentityTests(unittest.TestCase):
    def test_ssh_and_https_are_equivalent(self) -> None:
        for url in (CANONICAL_HTTPS, CANONICAL_SSH, CANONICAL_SSH_URI, CANONICAL_HTTPS.removesuffix(".git")):
            with self.subTest(url=url):
                self.assertEqual(MODULE.github_repo_identity(url), MODULE.CANONICAL_REPO)
                MODULE.require_canonical_url(url, what="origin")

    def test_wrong_host_owner_and_repository_are_rejected(self) -> None:
        repo = MODULE.CANONICAL_REPO
        name = repo.rsplit("/", 1)[-1]
        for url in (
            "https://gitlab.com/" + repo + ".git",
            "https://github.com.evil.example/" + repo + ".git",
            "https://github.com/other/" + name + ".git",
            "https://github.com/" + repo.rsplit("/", 1)[0] + "/other.git",
            "git@github.com:" + repo.rsplit("/", 1)[0] + "/" + repo.rsplit("/", 1)[0] + ".git",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(MODULE.MonitorError, "canonical GitHub repository"):
                    MODULE.require_canonical_url(url, what="origin")

    def test_transport_root_rejects_extra_path_bare_name_and_newlines(self) -> None:
        for value in (
            CANONICAL_HTTPS.removesuffix(".git") + "/another.git",
            CANONICAL_SSH.removesuffix(".git") + "/another.git",
            MODULE.CANONICAL_REPO,
            CANONICAL_HTTPS + "\nfile:///tmp/another",
        ):
            with self.subTest(value=value):
                with self.assertRaises(MODULE.MonitorError):
                    MODULE.require_canonical_url(value, what="independent origin")
                self.assertEqual(MODULE.github_repo_identity(value), "")


class LocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_absent_clone_is_an_error_for_status(self) -> None:
        dest = self.root / "missing"
        pin = {"commit": "a" * 40, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        with self.assertRaisesRegex(MODULE.MonitorError, "no monitor checkout"):
            MODULE.locate_checkout(dest, create=False, pin=pin, url=CANONICAL_HTTPS)

    def test_valid_configured_checkout_is_accepted(self) -> None:
        clone = self.root / "clone"
        commit = init_canonical_repo(clone, CANONICAL_SSH)
        pin = {"commit": commit, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        found, created = MODULE.locate_checkout(clone, create=False, pin=pin, url=CANONICAL_HTTPS)
        self.assertTrue(MODULE.same_path(found, clone))
        self.assertFalse(created)
        self.assertEqual(MODULE.validate_existing_checkout(clone, pin, require_clean_for_ensure=True), commit)

    def test_canonical_https_origin_is_accepted(self) -> None:
        clone = self.root / "https-clone"
        commit = init_canonical_repo(clone, CANONICAL_HTTPS)
        pin = {"commit": commit, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        MODULE.validate_existing_checkout(clone, pin, require_clean_for_ensure=True)

    def test_wrong_origin_is_rejected_without_mutating(self) -> None:
        clone = self.root / "wrong"
        commit = init_canonical_repo(
            clone, "https://github.com/other/" + MODULE.CANONICAL_REPO.rsplit("/", 1)[-1] + ".git"
        )
        before = git(clone, "rev-parse", "HEAD").stdout.strip()
        origin = git(clone, "remote", "get-url", "origin").stdout.strip()
        pin = {"commit": commit, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        with self.assertRaisesRegex(MODULE.MonitorError, "canonical GitHub repository"):
            MODULE.locate_checkout(clone, create=True, pin=pin, url=CANONICAL_HTTPS)
        self.assertEqual(git(clone, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(git(clone, "remote", "get-url", "origin").stdout.strip(), origin)

    def test_nested_path_is_rejected(self) -> None:
        repo = self.root / "outer"
        repo.mkdir()
        git(repo, "init")
        nested = repo / "nested"
        nested.mkdir()
        pin = {"commit": "a" * 40, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        with self.assertRaisesRegex(MODULE.MonitorError, "nested checkout path"):
            MODULE.locate_checkout(nested, create=True, pin=pin, url=CANONICAL_HTTPS)
        self.assertTrue(nested.is_dir())
        self.assertFalse((nested / ".git").exists())

    def test_non_git_nonempty_directory_is_rejected(self) -> None:
        dest = self.root / "nongit"
        dest.mkdir()
        (dest / "keep.txt").write_text("stay\n", encoding="utf-8")
        pin = {"commit": "a" * 40, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        with self.assertRaisesRegex(MODULE.MonitorError, "non-Git directory is not empty"):
            MODULE.locate_checkout(dest, create=True, pin=pin, url=CANONICAL_HTTPS)
        self.assertEqual((dest / "keep.txt").read_text(encoding="utf-8"), "stay\n")

    def test_legacy_scaffold_worktree_is_rejected_without_mutation(self) -> None:
        scaffold = self.root / "scaffold"
        scaffold.mkdir()
        git(scaffold, "init")
        git(scaffold, "config", "user.email", "test@example.com")
        git(scaffold, "config", "user.name", "Test")
        (scaffold / "README.md").write_text("scaffold\n", encoding="utf-8")
        git(scaffold, "add", "README.md")
        git(scaffold, "commit", "-m", "init")
        dest = self.root / "legacy-worktree"
        git(scaffold, "worktree", "add", "--detach", str(dest))
        (dest / "data").mkdir()
        (dest / "data" / "private.sqlite").write_text("secret-runtime\n", encoding="utf-8")
        pin = {"commit": "a" * 40, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        with mock.patch.object(MODULE, "REPO_ROOT", scaffold):
            with self.assertRaisesRegex(MODULE.MonitorError, "legacy scaffold"):
                MODULE.locate_checkout(dest, create=True, pin=pin, url=CANONICAL_HTTPS)
        self.assertEqual((dest / "data" / "private.sqlite").read_text(encoding="utf-8"), "secret-runtime\n")
        self.assertEqual(git(dest, "rev-parse", "--is-inside-work-tree").stdout.strip(), "true")
        self.assertEqual(
            git(scaffold, "branch", "--list", MODULE.CANONICAL_REPO.rsplit("/", 1)[-1]).stdout.strip(),
            "",
        )

    def test_dirty_and_untracked_source_are_preserved(self) -> None:
        clone = self.root / "dirty"
        commit = init_canonical_repo(clone)
        pin = {"commit": commit, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        (clone / "package.json").write_text("changed\n", encoding="utf-8")
        (clone / "extra.py").write_text("print('untracked')\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.MonitorError, "source changes"):
            MODULE.validate_existing_checkout(clone, pin, require_clean_for_ensure=True)
        self.assertEqual((clone / "package.json").read_text(encoding="utf-8"), "changed\n")
        self.assertEqual((clone / "extra.py").read_text(encoding="utf-8"), "print('untracked')\n")
        self.assertEqual(
            MODULE.validate_existing_checkout(clone, pin, require_clean_for_ensure=False),
            commit,
        )

    def test_ignored_runtime_data_and_env_are_not_source_dirt(self) -> None:
        clone = self.root / "runtime"
        commit = init_canonical_repo(clone)
        (clone / "data").mkdir()
        (clone / "data" / "state.sqlite").write_text("runtime\n", encoding="utf-8")
        (clone / ".env").write_text("NFM_BIND=127.0.0.1\n", encoding="utf-8")
        pin = {"commit": commit, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        self.assertEqual(MODULE.git_source_dirty(clone), "")
        MODULE.validate_existing_checkout(clone, pin, require_clean_for_ensure=True)

    def test_missing_pin_fails_closed(self) -> None:
        missing = self.root / "absent-pin.json"
        with self.assertRaisesRegex(MODULE.MonitorError, "dependency pin is missing"):
            MODULE.load_pin(missing)

    def test_pin_without_commit_is_not_an_unpinned_fallback(self) -> None:
        path = self.root / "pin.json"
        write_pin(path, "a" * 40)
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["commit"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.MonitorError, "no exact commit"):
            MODULE.load_pin(path)

    def test_divergent_checkout_is_not_advanced(self) -> None:
        clone = self.root / "divergent"
        commit = init_canonical_repo(clone)
        pin = {"commit": "b" * 40, "repository": MODULE.CANONICAL_REPO, "url": CANONICAL_HTTPS}
        with self.assertRaisesRegex(MODULE.MonitorError, "silently advance"):
            MODULE.validate_existing_checkout(clone, pin, require_clean_for_ensure=True)
        self.assertEqual(git(clone, "rev-parse", "HEAD").stdout.strip(), commit)


class EnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clone = Path(self.temp.name) / "clone"
        self.clone.mkdir()
        self.addCleanup(self.temp.cleanup)

    def test_atomic_narrow_update_preserves_unrelated_keys(self) -> None:
        env_path = self.clone / ".env"
        env_path.write_text(
            "NFM_BIND=127.0.0.1\n"
            "NFM_STATE_DIR=/custom/state\n"
            "NFM_INVENTORY_FILES=/old/inventory.json\n"
            "CUSTOM=keep-me\n",
            encoding="utf-8",
        )
        MODULE.apply_clone_env(
            self.clone,
            inventory_files=[Path("/new/inventory.json")],
            host_pool_files=[Path("/new/hosts.txt")],
            bootstrap_command="{python} /bin/true --host {host}",
            explicit={"NFM_HOST_POOL_FILES", "NFM_BOOTSTRAP_COMMAND"},
        )
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("NFM_BIND=127.0.0.1", text)
        self.assertIn("NFM_STATE_DIR=/custom/state", text)
        self.assertIn("CUSTOM=keep-me", text)
        self.assertIn("NFM_INVENTORY_FILES=/old/inventory.json", text)
        self.assertIn("NFM_HOST_POOL_FILES=/new/hosts.txt", text)
        self.assertNotIn("password", text.lower())

    def test_explicit_override_wins_over_existing_env(self) -> None:
        (self.clone / ".env").write_text("NFM_INVENTORY_FILES=/old.json\n", encoding="utf-8")
        MODULE.apply_clone_env(
            self.clone,
            inventory_files=[Path("/explicit.json")],
            host_pool_files=[],
            bootstrap_command="{python} /bin/true --host {host}",
            explicit={"NFM_INVENTORY_FILES"},
        )
        self.assertIn("NFM_INVENTORY_FILES=/explicit.json", (self.clone / ".env").read_text(encoding="utf-8"))

    def test_spaces_and_backslashes_are_quoted(self) -> None:
        inventory = Path("/tmp/inventory dir/machine-inventory.json")
        encoded = MODULE.encode_env_value(str(inventory))
        self.assertTrue(encoded.startswith('"'))
        self.assertIn("inventory dir", encoded)
        backslash = MODULE.encode_env_value("C:\\data\\npu")
        self.assertIn("\\\\", backslash)
        self.assertEqual(MODULE._unescape_env_value(backslash), "C:\\data\\npu")

    def test_newline_and_delimiter_injection_are_rejected(self) -> None:
        with self.assertRaisesRegex(MODULE.MonitorError, "newline"):
            MODULE.parse_path_list("/tmp/a.json\nNFM_BOOTSTRAP_COMMAND=evil", label="NFM_INVENTORY_FILES")
        with self.assertRaisesRegex(MODULE.MonitorError, "path-list delimiter"):
            MODULE._reject_ambiguous_path(Path(f"/tmp/a{os.pathsep}b.json"), label="inventory")
        with self.assertRaisesRegex(MODULE.MonitorError, "newline"):
            MODULE.encode_env_value("one\ntwo")

    def test_unknown_double_quote_escape_is_preserved_without_override(self) -> None:
        path = self.clone / ".env"
        value = "/tmp/owned\\q.json"
        path.write_text('NFM_INVENTORY_FILES="' + value + '"\nUNRELATED=keep\n', encoding="utf-8")
        self.assertEqual(MODULE.existing_env_map(path)["NFM_INVENTORY_FILES"], value)
        MODULE.apply_clone_env(
            self.clone,
            inventory_files=[Path("/tmp/default.json")],
            host_pool_files=[],
            bootstrap_command="",
            explicit=set(),
        )
        self.assertEqual(MODULE.existing_env_map(path)["NFM_INVENTORY_FILES"], value)
        text = path.read_text(encoding="utf-8")
        self.assertIn('NFM_INVENTORY_FILES="' + value + '"', text)
        self.assertIn("UNRELATED=keep", text)

    def test_progress_does_not_log_env_contents(self) -> None:
        messages: list[str] = []
        with mock.patch.object(MODULE, "progress", side_effect=messages.append):
            MODULE.apply_clone_env(
                self.clone,
                inventory_files=[Path("/tmp/machine-inventory.json")],
                host_pool_files=[],
                bootstrap_command="{python} /bin/true --host {host}",
                explicit=set(MODULE.CONSUMER_ENV_KEYS),
            )
        joined = "\n".join(messages)
        self.assertNotIn("/tmp/machine-inventory.json", joined)
        self.assertNotIn("{python}", joined)


class InventoryPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.primary = Path(self.temp.name) / "primary"
        self.linked = Path(self.temp.name) / "linked"
        self.addCleanup(self.temp.cleanup)
        self.primary.mkdir()
        git(self.primary, "init")
        git(self.primary, "config", "user.email", "test@example.com")
        git(self.primary, "config", "user.name", "Test")
        (self.primary / "README.md").write_text("scaffold\n", encoding="utf-8")
        git(self.primary, "add", "README.md")
        git(self.primary, "commit", "-m", "init")
        git(self.primary, "worktree", "add", "--detach", str(self.linked))

    def test_linked_worktree_uses_shared_inventory_and_optional_compat_file(self) -> None:
        shared = self.primary / ".vaws-local" / "machine-inventory.json"
        shared.parent.mkdir(parents=True)
        shared.write_text('{"machines":[]}\n', encoding="utf-8")
        legacy = self.primary / ".machine-inventory.json"
        legacy.write_text('{"machines":[]}\n', encoding="utf-8")
        stale = self.linked / ".vaws-local" / "machine-inventory.json"
        stale.parent.mkdir(parents=True)
        stale.write_text('{"machines":[{"alias":"stale"}]}\n', encoding="utf-8")
        files = MODULE.default_inventory_files(self.linked)
        self.assertEqual(files[0].resolve(), shared.resolve())
        self.assertIn(legacy.resolve(), [item.resolve() for item in files])
        self.assertNotIn(stale.resolve(), [item.resolve() for item in files])
        self.assertFalse(any("stale" in str(item) and item == stale for item in files))

    def test_host_pool_is_omitted_when_absent(self) -> None:
        self.assertEqual(MODULE.default_host_pool_files(self.primary), [])
        hosts = self.primary / "hosts.txt"
        hosts.write_text("192.0.2.10\n", encoding="utf-8")
        self.assertEqual(MODULE.default_host_pool_files(self.linked), [hosts.resolve()])


class BootstrapTemplateTests(unittest.TestCase):
    def test_spaces_and_placeholders_render_without_passwords(self) -> None:
        repo = Path("/tmp/repo with spaces/scaffold")
        template = MODULE.default_bootstrap_command(repo)
        argv = MODULE.render_bootstrap_argv(
            template,
            host="192.0.2.21",
            port="22",
            user="root",
            public_key_file="/tmp/key with spaces/id.pub",
            python="/usr/bin/python3",
        )
        self.assertEqual(argv[0], "/usr/bin/python3")
        self.assertEqual(
            argv[1],
            str(repo / ".agents" / "skills" / "machine-management" / "scripts" / "manage_machine.py"),
        )
        self.assertEqual(
            argv[2:],
            [
                "bootstrap-host-key",
                "--host",
                "192.0.2.21",
                "--host-port",
                "22",
                "--user",
                "root",
                "--public-key-file",
                "/tmp/key with spaces/id.pub",
                "--password-stdin",
            ],
        )
        self.assertNotIn("--password", argv)
        self.assertFalse(any("secret" in item.lower() for item in argv))
        calls: list[list[str]] = []

        def fake_runner(command: list[str]) -> int:
            calls.append(command)
            return 0

        self.assertEqual(fake_runner(argv), 0)
        self.assertEqual(calls, [argv])
        self.assertEqual(shlex.split(template)[2], "bootstrap-host-key")


class ReadOnlyActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clone = self.root / "clone"
        self.commit = init_canonical_repo(self.clone)
        self.pin_path = self.root / (MODULE.CANONICAL_REPO.rsplit("/", 1)[-1] + ".json")
        write_pin(self.pin_path, self.commit)
        self.addCleanup(self.temp.cleanup)
        self.recorded: list[list[str]] = []
        self.original_run = MODULE.run

        def recording_run(command: list[str], **kwargs: object) -> MODULE.subprocess_result:
            self.recorded.append(list(command))
            if command and Path(command[0]).name == "systemctl":
                return MODULE.subprocess_result(0, "ActiveState=inactive\nSubState=dead\n", "")
            return self.original_run(command, **kwargs)

        self.run_patch = mock.patch.object(MODULE, "run", side_effect=recording_run)
        self.pin_patch = mock.patch.object(MODULE, "DEPENDENCY_FILE", self.pin_path)
        self.health_patch = mock.patch.object(MODULE, "health", return_value=(True, {"status": "ok"}, None))
        self.systemd_patch = mock.patch.object(
            MODULE, "systemd_properties", return_value={"ActiveState": "active"}
        )
        self.run_patch.start()
        self.pin_patch.start()
        self.health_patch.start()
        self.systemd_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.addCleanup(self.pin_patch.stop)
        self.addCleanup(self.health_patch.stop)
        self.addCleanup(self.systemd_patch.stop)

    def _mutating(self) -> list[list[str]]:
        blocked = []
        for command in self.recorded:
            name = Path(command[0]).name
            joined = " ".join(command)
            if name in {"npm", "npm.cmd", "node"}:
                blocked.append(command)
            if name.endswith("install-user-service.sh"):
                blocked.append(command)
            if name == "git" and any(token in {"clone", "fetch", "checkout", "reset", "worktree"} for token in command):
                blocked.append(command)
            if "git clone" in joined or "git fetch" in joined:
                blocked.append(command)
        return blocked

    def test_status_never_clones_fetches_or_builds(self) -> None:
        code = MODULE.main(["status", "--clone-dir", str(self.clone)])
        self.assertEqual(code, 0)
        self.assertEqual(self._mutating(), [])

    def test_restart_and_stop_do_not_install_or_build(self) -> None:
        with mock.patch.object(MODULE, "systemd_properties", return_value={"ActiveState": "inactive"}):
            self.assertEqual(MODULE.main(["stop", "--clone-dir", str(self.clone)]), 0)
        self.assertEqual(MODULE.main(["restart", "--clone-dir", str(self.clone)]), 0)
        self.assertEqual(self._mutating(), [])

    def test_help_does_not_touch_git(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout):
            with self.assertRaises(SystemExit) as ctx:
                MODULE.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(self.recorded, [])


class EnsureActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture"
        self.commit = init_canonical_repo(self.fixture)
        self.pin_path = self.root / "pin.json"
        write_pin(self.pin_path, self.commit)
        self.addCleanup(self.temp.cleanup)

    def test_ensure_clones_only_when_absent_and_does_not_allocate(self) -> None:
        dest = self.root / "fresh"
        cloned: list[tuple[str, Path, str]] = []

        def fake_clone(url: str, target: Path, commit: str) -> None:
            cloned.append((url, target, commit))
            shutil.copytree(self.fixture, target)

        with mock.patch.object(MODULE, "DEPENDENCY_FILE", self.pin_path), mock.patch.object(
            MODULE, "clone_repository", side_effect=fake_clone
        ), mock.patch.object(MODULE, "build_if_needed", return_value=False) as built, mock.patch.object(
            MODULE, "install_and_restart"
        ) as installed, mock.patch.object(
            MODULE, "health", return_value=(True, {"status": "ok"}, None)
        ), mock.patch.object(
            MODULE, "systemd_properties", return_value={"ActiveState": "active"}
        ), mock.patch.object(
            MODULE, "REPO_ROOT", self.root / "scaffold"
        ):
            (self.root / "scaffold").mkdir()
            stdout = io.StringIO()
            with mock.patch.object(sys, "stdout", stdout):
                code = MODULE.main(["ensure", "--clone-dir", str(dest)])
        self.assertEqual(code, 0)
        self.assertEqual(len(cloned), 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["allocation_authority"])
        self.assertNotIn("machines", payload)
        self.assertNotIn("lease", json.dumps(payload))
        built.assert_called_once()
        installed.assert_called_once()

    def test_ensure_on_existing_pin_does_not_clone(self) -> None:
        with mock.patch.object(MODULE, "DEPENDENCY_FILE", self.pin_path), mock.patch.object(
            MODULE, "clone_repository", side_effect=AssertionError("clone")
        ), mock.patch.object(MODULE, "build_if_needed", return_value=False), mock.patch.object(
            MODULE, "install_and_restart"
        ), mock.patch.object(
            MODULE, "health", return_value=(True, {"status": "ok"}, None)
        ), mock.patch.object(
            MODULE, "systemd_properties", return_value={"ActiveState": "active"}
        ), mock.patch.object(
            MODULE, "REPO_ROOT", self.root / "scaffold"
        ):
            (self.root / "scaffold").mkdir()
            code = MODULE.main(["ensure", "--clone-dir", str(self.fixture)])
        self.assertEqual(code, 0)


class LocatorMetadataTests(unittest.TestCase):
    def test_cli_env_and_default_sources(self) -> None:
        requested = Path("/tmp/explicit-top")
        path, source = MODULE.resolve_clone_dir(requested)
        self.assertEqual(source, "cli")
        self.assertEqual(path, requested.resolve())
        path, source = MODULE.resolve_clone_dir(None, env={MODULE.CLONE_ROOT_ENV: "/tmp/env-top"})
        self.assertEqual(source, "env")
        self.assertEqual(path, Path("/tmp/env-top").resolve())
        with mock.patch.object(MODULE, "default_clone_dir", return_value=Path("/tmp/default-top")):
            path, source = MODULE.resolve_clone_dir(None, env={})
        self.assertEqual(source, "default")
        self.assertEqual(path, Path("/tmp/default-top").resolve())


if __name__ == "__main__":
    unittest.main()
