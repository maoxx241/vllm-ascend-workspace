#!/usr/bin/env python3
"""Regression tests for the tracked-file leak guard.

Everything here runs on a laptop: temporary Git repositories, text fixtures,
and the repository's own tracked tree. No SSH, no NPU, no torch.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import vaws_leak_guard as guard  # noqa: E402

FIXTURE_DIR = ROOT / ".agents" / "tests" / "fixtures" / "tracked_leak_guard"
POLICY_PATH = ROOT / ".agents" / "leak-guard" / "allowlist.yaml"
SCANNER = ROOT / ".agents" / "scripts" / "tracked_leak_scan.py"
HOOK = ROOT / ".agents" / "hooks" / "tracked_leak_precommit.py"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def scan_fixture(name: str, policy: guard.Policy | None = None) -> list[guard.Finding]:
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return guard.scan_text(text, path=f"fixture/{name}", policy=policy or guard.default_policy())


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def init_repo(repo: Path) -> None:
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")


class DetectionTests(unittest.TestCase):
    """The dirty fixture must produce at least one finding per category."""

    def test_every_category_is_detected(self) -> None:
        findings = scan_fixture("dirty.txt")
        found = {finding.category for finding in findings}
        for category in guard.CATEGORIES:
            with self.subTest(category=category):
                self.assertIn(category, found)

    def test_clean_fixture_produces_no_findings(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        # Reuse the repository policy's allowed prefixes without its
        # fixture exclusion, so allowed values are proven allowed by policy.
        policy.scoped_exclusions = ()
        findings = scan_fixture("clean.txt", policy)
        self.assertEqual(
            [guard.format_finding_line(item, show_matches=True) for item in findings], []
        )

    def test_documentation_and_loopback_ranges_are_allowed(self) -> None:
        policy = guard.default_policy()
        for value in ("127.0.0.1", "192.0.2.10", "198.51.100.4", "203.0.113.9", "0.0.0.0"):
            with self.subTest(value=value):
                self.assertEqual(guard.scan_line(f"host = {value}", policy), [])
        for value in ("::1", "2001:db8::1"):
            with self.subTest(value=value):
                self.assertEqual(guard.scan_line(f"host = {value}", policy), [])

    def test_private_and_public_addresses_are_reported(self) -> None:
        policy = guard.default_policy()
        for value in ("192.168.240.7", "10.11.12.13", "100.64.0.1", "fd00:beef::12"):
            with self.subTest(value=value):
                spans = guard.scan_line(f"host = {value}", policy)
                self.assertEqual([span[2] for span in spans], ["ipv4" if "." in value else "ipv6"])

    def test_named_paths_are_reported_and_shared_mounts_are_not(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        for value in ("/Users/janedoe/x", "/home/jsmith/x", "/root/jsmith/x"):
            with self.subTest(value=value):
                spans = guard.scan_line(value, policy)
                self.assertEqual([span[2] for span in spans], ["absolute-user-path"])
        for value in (
            "/vllm-workspace/.vaws-runtime",
            "/home/weights/Qwen3-32B",
            "/root/Qwen3-32B",
            "/root/.cache/hub",
            "/Users/Shared/x",
        ):
            with self.subTest(value=value):
                self.assertEqual(guard.scan_line(value, policy), [])

    def test_scanned_roots_are_policy_not_code(self) -> None:
        policy = guard.default_policy()
        self.assertEqual(guard.scan_line("/vllm-workspace/model", policy), [])
        policy.user_path_re = guard.build_user_path_re(("Users", "home", "root", "vllm-workspace"))
        spans = guard.scan_line("/vllm-workspace/model", policy)
        self.assertEqual([span[2] for span in spans], ["absolute-user-path"])

    def test_credential_shaped_values_extend_knowledge_patterns(self) -> None:
        policy = guard.default_policy()
        for line in (
            'api_key = "Zt7Qw2Lm9Rk4Xy1B"',
            "password: 8Hj2kLm9Qw4Z",
            "AWS key AKIAIOSFODNN7EXAMPLE",
            "Authorization: Bearer aaaabbbbccccdddd1234",
        ):
            with self.subTest(line=line):
                spans = guard.scan_line(line, policy)
                self.assertTrue(
                    any(span[2] in {"secret-key", "secret-value"} for span in spans), spans
                )

    def test_code_shaped_assignments_are_not_credentials(self) -> None:
        policy = guard.default_policy()
        for line in (
            "token = invocation_id",
            "password=password",
            'token_env = "VAWS_COORDINATOR_TOKEN"',
            'token_file = "/private/path/to/token"',
            'bootstrap = "password-once"',
            'password = "<your-password>"',
            "secret = os.environ.get(name)",
        ):
            with self.subTest(line=line):
                self.assertEqual(guard.scan_line(line, policy), [])

    def test_hostname_rule_ignores_python_attribute_access(self) -> None:
        policy = guard.default_policy()
        self.assertEqual(guard.scan_line("value = spec.local", policy), [])
        self.assertEqual(guard.scan_line("if args.local_path:", policy), [])
        for line in ('host = "registry.local"', "Host node.internal:22", "http://npu-01.corp/"):
            with self.subTest(line=line):
                spans = guard.scan_line(line, policy)
                self.assertEqual([span[2] for span in spans], ["internal-hostname"])
        # An ssh target reads as a mailbox; either category blocks the commit.
        spans = guard.scan_line("ssh root@node.internal", policy)
        self.assertEqual([span[2] for span in spans], ["email"])

    def test_identity_token_rule_ignores_short_git_shas(self) -> None:
        policy = guard.default_policy()
        self.assertEqual(guard.scan_line('"commit:c015161"', policy), [])
        spans = guard.scan_line("owner q12345678 wrote this", policy)
        self.assertEqual([span[2] for span in spans], ["internal-identifier"])

    def test_overlapping_rules_yield_one_finding(self) -> None:
        policy = guard.default_policy()
        spans = guard.scan_line("/home/q12345678/work", policy)
        self.assertEqual([span[2] for span in spans], ["absolute-user-path"])

    def test_preview_redacts_the_matched_value(self) -> None:
        finding = guard.Finding("a.md", 1, 1, "ipv4", "ipv4-address", "192.168.240.7")
        self.assertNotIn("240", finding.preview)
        self.assertIn("192", finding.preview)


class PolicyTests(unittest.TestCase):
    def test_repository_policy_loads_and_requires_justifications(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        self.assertTrue(policy.entries)
        self.assertTrue(policy.scoped_exclusions)
        for entry in policy.entries:
            with self.subTest(entry=entry.id):
                self.assertGreaterEqual(
                    len(entry.justification), guard.MIN_JUSTIFICATION_CHARS
                )
        for exclusion in policy.scoped_exclusions:
            with self.subTest(exclusion=exclusion.id):
                self.assertGreaterEqual(
                    len(exclusion.justification), guard.MIN_JUSTIFICATION_CHARS
                )

    def test_fixtures_are_excluded_by_scope_not_by_weakened_patterns(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        relative = (FIXTURE_DIR / "dirty.txt").relative_to(ROOT).as_posix()
        self.assertTrue(any(item.covers(relative) for item in policy.scoped_exclusions))
        # The same content outside the fixture directory is still reported.
        text = (FIXTURE_DIR / "dirty.txt").read_text(encoding="utf-8")
        self.assertTrue(guard.scan_text(text, path="docs/elsewhere.md", policy=policy))
        self.assertEqual(guard.scan_text(text, path=relative, policy=policy), [])

    def _write_policy(self, directory: Path, body: str) -> Path:
        path = directory / "allowlist.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_entry_without_justification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: no-reason\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv4]\n",
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "justification"):
                guard.load_policy(path)

    def test_short_justification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: terse\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv4]\n"
                "    justification: fine\n",
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "at least"):
                guard.load_policy(path)

    def test_unknown_category_and_duplicate_id_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: bad-category\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv7]\n"
                "    justification: this justification is long enough to pass\n",
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "unknown category"):
                guard.load_policy(path)
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: twice\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv4]\n"
                "    justification: this justification is long enough to pass\n"
                "  - id: twice\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv6]\n"
                "    justification: this justification is long enough to pass\n",
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "duplicate"):
                guard.load_policy(path)

    def test_unknown_keys_and_schema_version_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(Path(tmp), "schema_version: 2\n")
            with self.assertRaisesRegex(guard.LeakGuardError, "schema_version"):
                guard.load_policy(path)
            path = self._write_policy(
                Path(tmp), "schema_version: 1\nallow_everything: true\n"
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "unsupported keys"):
                guard.load_policy(path)

    def test_missing_policy_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(guard.LeakGuardError, "not found"):
                guard.load_policy(Path(tmp) / "absent.yaml")

    def test_allowlist_entry_scopes_to_path_category_and_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: docs-only\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv4]\n"
                "    match: 192.168.240.7\n"
                "    justification: reserved private example used by the guard test suite\n",
            )
            policy = guard.load_policy(path)
            line = "host 192.168.240.7"
            allowed = guard.scan_text(line, path="docs/a.md", policy=policy)
            self.assertEqual([item.allowlisted_by for item in allowed], ["docs-only"])
            elsewhere = guard.scan_text(line, path="src/a.py", policy=policy)
            self.assertEqual([item.allowlisted_by for item in elsewhere], [None])
            other_value = guard.scan_text("host 192.168.240.8", path="docs/a.md", policy=policy)
            self.assertEqual([item.allowlisted_by for item in other_value], [None])

    def test_yaml_fallback_parser_matches_pyyaml(self) -> None:
        text = POLICY_PATH.read_text(encoding="utf-8")
        fallback = guard._parse_yaml_subset(text)
        try:
            import yaml
        except ImportError:  # pragma: no cover - environment dependent
            self.skipTest("PyYAML is not installed")
        self.assertEqual(fallback, yaml.safe_load(text))


class DiffModeTests(unittest.TestCase):
    def test_staged_diff_reports_added_lines_with_post_image_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            target = repo / "notes.md"
            target.write_text("line one\nline two\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "base")
            target.write_text("line one\nline two\nhost 192.168.240.7\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            result = guard.scan_diff(guard.staged_diff(repo), guard.default_policy())
            self.assertEqual(len(result.findings), 1)
            finding = result.findings[0]
            self.assertEqual((finding.path, finding.line, finding.category), ("notes.md", 3, "ipv4"))

    def test_removing_a_leak_is_not_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            target = repo / "notes.md"
            target.write_text("host 192.168.240.7\nkeep\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "base")
            target.write_text("keep\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            result = guard.scan_diff(guard.staged_diff(repo), guard.default_policy())
            self.assertEqual(result.findings, [])

    def test_commit_range_mode_scans_added_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            target = repo / "notes.md"
            target.write_text("clean\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD").strip()
            target.write_text("clean\nmac 02:1a:2b:3c:4d:5e\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "leak")
            result = guard.scan_diff(guard.range_diff(repo, f"{base}..HEAD"), guard.default_policy())
            self.assertEqual([item.category for item in result.findings], ["mac-address"])

    def test_unsafe_commit_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(guard.LeakGuardError, "unsafe commit range"):
            guard.range_diff(ROOT, "main; rm -rf /")


class TrackedTreeTests(unittest.TestCase):
    def test_submodule_gitlinks_are_not_listed(self) -> None:
        paths = guard.tracked_files(ROOT)
        self.assertTrue(paths)
        self.assertNotIn("vllm", paths)
        self.assertNotIn("vllm-ascend", paths)
        self.assertFalse([path for path in paths if path.startswith(("vllm/", "vllm-ascend/"))])

    def test_binary_and_oversized_files_are_skipped_not_silently_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "blob.bin").write_bytes(b"\x00\x01host 192.168.240.7")
            (repo / "big.txt").write_text("host 192.168.240.7\n" * 100, encoding="utf-8")
            policy = guard.default_policy()
            policy.max_file_bytes = 64
            result = guard.scan_files(repo, ["blob.bin", "big.txt"], policy)
            self.assertEqual(result.findings, [])
            self.assertEqual(
                sorted(item["reason"] for item in result.skipped),
                ["binary", "larger-than-64-bytes"],
            )

    def test_repository_tracked_tree_is_clean_under_policy(self) -> None:
        """The gate CI enforces: no unallowlisted finding in the tracked tree."""

        policy = guard.load_policy(POLICY_PATH)
        result = guard.scan_files(ROOT, guard.tracked_files(ROOT), policy)
        self.assertEqual(
            [guard.format_finding_line(item, show_matches=False) for item in result.findings],
            [],
        )


class ScannerCliTests(unittest.TestCase):
    def _run(self, *args: str) -> tuple[int, dict, str]:
        result = subprocess.run(
            [sys.executable, "-B", str(SCANNER), *args],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        text = result.stdout.strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = json.loads(text.splitlines()[-1])
        return result.returncode, payload, result.stderr

    def test_progress_on_stderr_and_single_json_on_stdout(self) -> None:
        code, payload, stderr = self._run("--paths", ".agents/tests/fixtures/tracked_leak_guard/clean.txt")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertIn("[tracked-leak-scan]", stderr)

    def test_scoped_exclusion_keeps_the_guards_own_fixtures_clean(self) -> None:
        code, payload, _ = self._run(
            "--paths", ".agents/tests/fixtures/tracked_leak_guard/dirty.txt"
        )
        self.assertEqual((code, payload["finding_count"]), (0, 0))

    def test_findings_fail_with_exit_code_one(self) -> None:
        code, payload, _ = self._run(
            "--paths", ".agents/tests/fixtures/tracked_leak_guard/dirty.txt", "--no-allowlist"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertGreaterEqual(payload["finding_count"], len(guard.CATEGORIES))

    def test_tracked_tree_scan_passes_and_reports_suppressions(self) -> None:
        code, payload, _ = self._run("--format", "json")
        self.assertEqual((code, payload["status"]), (0, "passed"))
        self.assertEqual(payload["mode"], "tracked-tree")
        self.assertGreater(payload["suppressed_count"], 0)
        self.assertEqual(payload["unused_allowlist_entries"], [])


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hook = load_script_module("_vaws_leak_hook_test", HOOK)

    def _repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        init_repo(repo)
        policy_dir = repo / ".agents" / "leak-guard"
        policy_dir.mkdir(parents=True)
        (policy_dir / "allowlist.yaml").write_text(
            "schema_version: 1\n"
            "allowlist:\n"
            "  - id: docs-example\n"
            "    path_glob: allowed.md\n"
            "    categories: [ipv4]\n"
            "    match: 192.168.240.7\n"
            "    justification: reserved private example kept for the guard test suite\n",
            encoding="utf-8",
        )
        return repo

    def test_install_status_and_uninstall_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            payload = self.hook.install(repo, force=False)
            self.assertEqual((payload["status"], payload["action"]), ("passed", "installed"))
            hook_path = Path(payload["hook_path"])
            self.assertTrue(hook_path.exists())
            self.assertTrue(hook_path.stat().st_mode & 0o111)
            self.assertEqual(self.hook.status(repo)["installed"], True)
            self.assertEqual(self.hook.install(repo, force=False)["action"], "reinstalled")
            self.assertEqual(self.hook.uninstall(repo)["action"], "removed")
            self.assertEqual(self.hook.status(repo)["installed"], False)

    def test_foreign_hook_is_preserved_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            hook_path = self.hook.hooks_dir(repo)
            hook_path.mkdir(parents=True, exist_ok=True)
            (hook_path / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
            blocked = self.hook.install(repo, force=False)
            self.assertEqual(blocked["status"], "blocked")
            self.assertIn("mine", (hook_path / "pre-commit").read_text(encoding="utf-8"))
            forced = self.hook.install(repo, force=True)
            self.assertEqual(forced["action"], "replaced")
            self.assertIn("mine", Path(forced["backup_path"]).read_text(encoding="utf-8"))
            self.assertEqual(self.hook.uninstall(repo)["action"], "removed")

    def test_hook_blocks_a_real_commit_and_names_file_line_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            self.hook.install(repo, force=False)
            (repo / "leak.md").write_text("intro\nhost 192.168.240.7\n", encoding="utf-8")
            git(repo, "add", "leak.md")
            attempt = subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "add leak"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(attempt.returncode, 0)
            self.assertIn("leak.md:2:", attempt.stderr)
            self.assertIn("ipv4", attempt.stderr)
            self.assertIn("allowlist.yaml", attempt.stderr)
            self.assertIn("justification", attempt.stderr)
            # The blocked value itself is not echoed back in full.
            self.assertNotIn("192.168.240.7", attempt.stderr)

    def test_hook_allows_a_commit_covered_by_the_repository_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            self.hook.install(repo, force=False)
            (repo / "allowed.md").write_text("host 192.168.240.7\n", encoding="utf-8")
            git(repo, "add", "allowed.md")
            done = subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "allowed example"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_hook_fails_closed_when_the_policy_is_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            (repo / ".agents" / "leak-guard" / "allowlist.yaml").write_text(
                "schema_version: 1\nallow_everything: true\n", encoding="utf-8"
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = self.hook.main(["--check", "--repo-root", str(repo)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "error")

    def test_hook_resolves_the_policy_of_the_committed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            self.assertEqual(
                self.hook.resolve_policy_path(repo, None),
                repo / ".agents" / "leak-guard" / "allowlist.yaml",
            )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self.hook.resolve_policy_path(Path(tmp), None), guard.DEFAULT_POLICY_PATH
            )


if __name__ == "__main__":
    unittest.main()
