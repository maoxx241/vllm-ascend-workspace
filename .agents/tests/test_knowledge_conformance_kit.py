#!/usr/bin/env python3
"""Portable client run of the vaws-knowledge conformance kit."""

from __future__ import annotations

import json
import os
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
    _gate_vectors_dir,
    _runner_path,
    _vectors_dir,
    build_client_kit_argv,
    packaged_kit_root,
    resolve_kit_root,
    run_client_kit,
)

ADAPTER = ROOT / ".agents" / "tests" / "knowledge_client_adapter.py"


class KitConfigurationTests(unittest.TestCase):
    def test_unconfigured_uses_the_installed_package(self) -> None:
        packaged = packaged_kit_root()
        if packaged is None:
            with self.assertRaises(KitUnconfigured) as caught:
                resolve_kit_root(repo_root=ROOT, environ={}, read_local_file=False)
            self.assertIn(f"{KIT_ROOT_ENV} is not set", str(caught.exception))
            return
        root = resolve_kit_root(repo_root=ROOT, environ={}, read_local_file=False)
        self.assertEqual(root, packaged)

    def test_runner_commands_quote_spaces(self) -> None:
        command = shlex.join(
            ["/opt/Python 3.12/python", "/tmp/dir with spaces/adapter.py", "hash"]
        )
        self.assertEqual(len(shlex.split(command)), 3)

    def test_conflicts_cmd_omitted_when_pinned_runner_lacks_flag(self) -> None:
        temp = tempfile.TemporaryDirectory()
        try:
            kit = Path(temp.name)
            (kit / "conformance").mkdir()
            (kit / "conformance" / "runner.py").write_text(
                "# pre-v0.1.0 runner has no conflicts gate\n",
                encoding="utf-8",
            )
            argv = build_client_kit_argv(kit, repo_root=ROOT)
            self.assertNotIn("--conflicts-cmd", argv)
        finally:
            temp.cleanup()

    def test_conflicts_cmd_passed_when_runner_declares_flag(self) -> None:
        temp = tempfile.TemporaryDirectory()
        try:
            kit = Path(temp.name)
            (kit / "conformance").mkdir()
            (kit / "conformance" / "runner.py").write_text(
                'parser.add_argument("--conflicts-cmd")\n',
                encoding="utf-8",
            )
            argv = build_client_kit_argv(kit, repo_root=ROOT)
            self.assertIn("--conflicts-cmd", argv)
        finally:
            temp.cleanup()

    def test_configured_missing_path_fails(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "missing-kit"
        with self.assertRaises(KitInvalid) as caught:
            resolve_kit_root(
                repo_root=ROOT,
                environ={KIT_ROOT_ENV: str(missing)},
                read_local_file=False,
            )
        self.assertIn("does not exist", str(caught.exception))

    def test_configured_stub_with_wrong_vector_count_fails(self) -> None:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "conformance" / "vectors").mkdir(parents=True)
        (root / "conformance" / "runner.py").write_text("# synthetic non-kit\n", encoding="utf-8")
        (root / "conformance" / "vectors" / "stub-0.yaml").write_text("id: synthetic\n", encoding="utf-8")
        try:
            with self.assertRaises(KitInvalid) as caught:
                resolve_kit_root(
                    repo_root=ROOT,
                    environ={KIT_ROOT_ENV: str(root)},
                    read_local_file=False,
                )
            self.assertIn("hash vectors", str(caught.exception))
        finally:
            temp.cleanup()

    def test_configured_complete_stub_is_accepted_without_git(self) -> None:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "conformance" / "vectors").mkdir(parents=True)
        (root / "conformance" / "runner.py").write_text("# stub\n", encoding="utf-8")
        for index in range(EXPECTED_VECTOR_COUNT):
            (root / "conformance" / "vectors" / f"stub-{index}.yaml").write_text(
                "id: synthetic\n", encoding="utf-8"
            )
        try:
            resolved = resolve_kit_root(
                repo_root=ROOT,
                environ={KIT_ROOT_ENV: str(root)},
                read_local_file=False,
            )
            self.assertEqual(resolved, root.resolve())
        finally:
            temp.cleanup()


class ConfiguredSharedKitTests(unittest.TestCase):
    """Integration against the installed package kit, or an explicit checkout."""

    def setUp(self) -> None:
        raw = os.environ.get(KIT_ROOT_ENV, "").strip()
        if raw and not Path(raw).expanduser().is_dir():
            self.skipTest(f"{KIT_ROOT_ENV} is {raw!r} but that path does not exist")
        try:
            self.kit = resolve_kit_root(repo_root=ROOT, read_local_file=False)
        except KitUnconfigured as exc:
            self.skipTest(str(exc))

    def test_configured_vectors_including_export(self) -> None:
        completed = run_client_kit(self.kit, repo_root=ROOT)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS  export-idempotent-unchanged-entry", completed.stdout)
        self.assertIn("PASS  export-idempotent-scrambled-key-order", completed.stdout)
        self.assertIn("PASS  redaction-email", completed.stdout)
        self.assertIn("conformance PASSED", completed.stdout)

    def test_redaction_email_vector_through_tracked_adapter(self) -> None:
        import yaml  # noqa: PLC0415

        vector = yaml.safe_load(
            (_gate_vectors_dir(self.kit) / "redaction-email.yaml").read_text(encoding="utf-8")
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

    def test_kit_layout_exposes_runner_and_nineteen_vectors(self) -> None:
        self.assertTrue(_runner_path(self.kit).is_file())
        self.assertEqual(len(list(_vectors_dir(self.kit).glob("*.yaml"))), EXPECTED_VECTOR_COUNT)


if __name__ == "__main__":
    unittest.main()
