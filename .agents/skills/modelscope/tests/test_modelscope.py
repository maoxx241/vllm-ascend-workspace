#!/usr/bin/env python3
"""Hermetic tests for modelscope path, resume/status, and SHA256 decision logic.

Official ModelScope HTTP is replaced with fixtures. Nothing here opens a
network socket or depends on the developer HOME.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "modelscope" / "scripts"
sys.path.insert(0, str(SCRIPTS))

# The skill scripts import ``requests`` at module load. Tests never call it;
# inject a stub so the suite stays hermetic without that extra package.
if "requests" not in sys.modules:
    sys.modules["requests"] = type(sys)("requests")
    sys.modules["requests"].Session = object  # type: ignore[attr-defined]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


auto = _load("vaws_modelscope_auto_under_test", "modelscope_auto.py")
download = _load("vaws_modelscope_download_under_test", "download_from_modelscope.py")
verify = _load("vaws_modelscope_verify_under_test", "verify_modelscope_sha256.py")
status_mod = _load("vaws_modelscope_status_under_test", "modelscope_download_status.py")


OFFICIAL = [
    {"Type": "blob", "Path": "config.json", "Size": 5, "Sha256": hashlib.sha256(b"hello").hexdigest()},
    {"Type": "blob", "Path": "weights.bin", "Size": 3, "Sha256": hashlib.sha256(b"abc").hexdigest()},
    {"Type": "blob", "Path": ".gitattributes", "Size": 1, "Sha256": "aa"},
]


def _spec(tmp: str, model_id: str = "org/name") -> auto.ModelSpec:
    return auto.ModelSpec(model_id=model_id, local_dir=Path(tmp) / "org" / "name")


def write_report(spec, files, revision="master"):
    with mock.patch.object(verify, "fetch_official_files", return_value=files):
        checks, summary = verify.verify_model(spec, revision, 4096, set(), {".gitattributes"})
    verify.write_outputs(spec.local_dir, "modelscope_sha256", checks, [summary])
    return checks


class ArgumentValidationTests(unittest.TestCase):
    def test_live_unrelated_or_reused_pid_is_not_an_active_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            pidfile = spec.local_dir / "download.pid"
            identity = auto.process_identity(os.getpid())
            self.assertIsNotNone(identity)
            for record in (os.getpid(), {"pid": os.getpid(), "identity": {**identity, "started": "other"}}):
                pidfile.write_text(json.dumps(record), encoding="utf-8")
                with mock.patch.object(auto, "fetch_official_files", return_value=OFFICIAL[:2]):
                    self.assertEqual(auto.inspect_model(spec, "master")["state"], "needs-download")
            pidfile.write_text(json.dumps({"pid": os.getpid(), "identity": identity}), encoding="utf-8")
            self.assertTrue(auto.worker_is_active(spec.local_dir, os.getpid()))

    def test_aggregate_report_filters_model_and_directory_and_rejects_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            (spec.local_dir / "config.json").write_bytes(b"hello")
            (spec.local_dir / "weights.bin").write_bytes(b"abc")
            write_report(spec, OFFICIAL[:2])
            path = spec.local_dir / "modelscope_sha256.report.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            report["checks"].append({**report["checks"][0], "model_id": "other/model", "status": "mismatch"})
            report["checks"].append({**report["checks"][0], "local_dir": str(Path(tmp) / "elsewhere")})
            report["all_ok"] = False
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(auto.report_state(spec.local_dir, model_id=spec.model_id, revision="master", files=OFFICIAL[:2]), "ok")
            report["checks"][0]["local_dir"] = None
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(auto.report_state(spec.local_dir, model_id=spec.model_id, revision="master", files=OFFICIAL[:2]), "invalid")

    def test_parse_model_spec_requires_namespace_and_local_dir(self) -> None:
        spec = auto.parse_model_spec("org/name=/tmp/weights")
        self.assertEqual(spec.model_id, "org/name")
        self.assertEqual(spec.local_dir, Path("/tmp/weights"))
        for bad in ("org/name", "orgname=/tmp/x", "org/name=", "= /tmp/x"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                auto.parse_model_spec(bad)
        with self.assertRaises(argparse.ArgumentTypeError):
            status_mod.parse_model_spec("not-a-spec")
        with self.assertRaises(argparse.ArgumentTypeError):
            verify.parse_model_spec("org/name")

    def test_positive_int_rejects_zero_and_negative(self) -> None:
        self.assertEqual(auto.positive_int("3"), 3)
        self.assertEqual(download.positive_int("2"), 2)
        for bad in ("0", "-1"):
            with self.subTest(module="auto", bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                auto.positive_int(bad)
            with self.subTest(module="download", bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                download.positive_int(bad)

    def test_resolve_models_requires_model_or_root(self) -> None:
        with self.assertRaises(SystemExit):
            auto.resolve_models(argparse.Namespace(model=None, root=None))


class PathHandlingTests(unittest.TestCase):
    def test_discover_models_reads_explicit_local_pid_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_dir = root / "Qwen" / "Qwen2.5-7B"
            model_dir.mkdir(parents=True)
            (model_dir / "download.pid").write_text("123\n", encoding="utf-8")
            (root / "download.pid").write_text("1\n", encoding="utf-8")
            specs = auto.discover_models(root)
            self.assertEqual([(s.model_id, s.local_dir) for s in specs], [("Qwen/Qwen2.5-7B", model_dir)])

    def test_local_size_counts_only_official_files_under_local_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_bytes(b"hello")
            (root / "extra.bin").write_bytes(b"xxxx")
            self.assertEqual(auto.local_size_for_files(root, OFFICIAL), 5)
            self.assertEqual(auto.local_size_for_files(root / "missing", OFFICIAL), 0)

    def test_configure_environment_stays_inside_explicit_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                no_proxy=True,
                proxy=None,
                download_parallels=2,
                parallel_threshold_mb=100,
                cache_dir=Path(tmp) / "cache",
            )
            old = os.environ.get("HTTP_PROXY")
            os.environ["HTTP_PROXY"] = "http://proxy.example.invalid:1"
            try:
                download.configure_environment(args)
                self.assertNotIn("HTTP_PROXY", os.environ)
                self.assertEqual(os.environ["MODELSCOPE_CACHE"], str(Path(tmp) / "cache"))
                self.assertEqual(os.environ["MODELSCOPE_DOWNLOAD_PARALLELS"], "2")
            finally:
                if old is None:
                    os.environ.pop("HTTP_PROXY", None)
                else:
                    os.environ["HTTP_PROXY"] = old


class StatusAndResumeTests(unittest.TestCase):
    def test_report_state_reads_local_verification_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            self.assertEqual(auto.report_state(local), "none")
            (local / "modelscope_sha256.report.json").write_text("{", encoding="utf-8")
            self.assertEqual(auto.report_state(local), "invalid")
            (local / "modelscope_sha256.report.json").write_text(
                json.dumps({"all_ok": True, "checks": []}), encoding="utf-8"
            )
            self.assertEqual(auto.report_state(local), "stale")
            (local / "modelscope_sha256.report.json").write_text(
                json.dumps(
                    {
                        "all_ok": False,
                        "checks": [{"status": "missing", "path": ".gitattributes"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(auto.report_state(local), "stale")
            (local / "modelscope_sha256.report.json").write_text(
                json.dumps(
                    {
                        "all_ok": False,
                        "checks": [{"status": "sha256_mismatch", "path": "weights.bin"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(auto.report_state(local), "stale")

    def test_inspect_model_state_machine_uses_local_dir_only(self) -> None:
        blobs = [item for item in OFFICIAL if item["Path"] != ".gitattributes"]
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            (spec.local_dir / "config.json").write_bytes(b"hello")
            (spec.local_dir / "weights.bin").write_bytes(b"abc")
            with (
                mock.patch.object(auto, "fetch_official_files", return_value=blobs),
                mock.patch.object(auto, "pid_is_active", return_value=False),
            ):
                complete = auto.inspect_model(spec, "master")
                self.assertEqual(complete["state"], "needs-verify")
                self.assertTrue(complete["complete"])
                write_report(spec, blobs)
                verified = auto.inspect_model(spec, "master")
                self.assertEqual(verified["state"], "verified")
            (spec.local_dir / "weights.bin").unlink()
            with (
                mock.patch.object(auto, "fetch_official_files", return_value=blobs),
                mock.patch.object(auto, "worker_is_active", return_value=True),
                mock.patch.object(auto, "read_pid", return_value=4242),
            ):
                active = auto.inspect_model(spec, "master")
            self.assertEqual(active["state"], "active")
            self.assertFalse(active["complete"])
            with (
                mock.patch.object(auto, "fetch_official_files", return_value=blobs),
                mock.patch.object(auto, "pid_is_active", return_value=False),
            ):
                missing = auto.inspect_model(spec, "master")
            self.assertEqual(missing["state"], "needs-download")

    def test_complete_requires_each_file_and_verification_expires_on_changes(self) -> None:
        blobs = [item for item in OFFICIAL if item["Path"] != ".gitattributes"]
        with tempfile.TemporaryDirectory() as tmp:
            spec = _spec(tmp)
            spec.local_dir.mkdir(parents=True)
            (spec.local_dir / "config.json").write_bytes(b"12345678")
            with mock.patch.object(auto, "fetch_official_files", return_value=blobs):
                observed = auto.inspect_model(spec, "master")
                self.assertEqual(observed["actual"], observed["expected"])
                self.assertFalse(observed["complete"])
            (spec.local_dir / "config.json").write_bytes(b"hello")
            weights = spec.local_dir / "weights.bin"
            weights.write_bytes(b"abc")
            write_report(spec, blobs)
            def observed(revision="master", files=blobs):
                return auto.report_state(spec.local_dir, model_id=spec.model_id, revision=revision, files=files)
            self.assertEqual(observed(), "ok")
            self.assertEqual(observed("other-revision"), "stale")
            self.assertEqual(observed(files=[{**item, "Sha256": "new-upstream-hash"} for item in blobs]), "stale")
            self.assertEqual(auto.report_state(spec.local_dir, model_id="different/repo", revision="master", files=blobs), "stale")
            before = weights.stat()
            weights.write_bytes(b"abd")
            os.utime(weights, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            self.assertEqual(observed(), "stale")
            write_report(spec, blobs)
            self.assertEqual(observed(), "failed")
            weights.unlink()
            self.assertEqual(observed(), "stale")

    def test_auto_install_uses_isolated_uv_without_modifying_selected_python(self) -> None:
        with mock.patch.object(download.importlib.util, "find_spec", return_value=None), \
                mock.patch.object(download.shutil, "which", return_value="uv"), \
                mock.patch.object(download.subprocess, "call", return_value=7) as launch, \
                mock.patch.object(sys, "argv", ["download_from_modelscope.py", "--auto-install", "--model-id", "org/name", "--local-dir", "weights with spaces"]):
            with self.assertRaises(SystemExit) as stopped:
                download.ensure_modelscope(True)
        self.assertEqual(stopped.exception.code, 7)
        command = launch.call_args.args[0]
        self.assertEqual(command[:6], ["uv", "run", "--no-project", "--with", "modelscope", "python"])
        self.assertNotIn("--auto-install", command)
        self.assertEqual(command[-1], "weights with spaces")

    def test_command_ensure_launches_download_or_verify_or_stops(self) -> None:
        spec = auto.ModelSpec("org/name", Path("/tmp/org/name"))
        launched: list[bool] = []

        def fake_launch(_spec, _args, *, verify_only: bool) -> int:
            launched.append(verify_only)
            return 99

        args = argparse.Namespace(
            model=[spec],
            root=None,
            revision="master",
            proxy=None,
            no_proxy=False,
            max_retries=3,
            max_workers=None,
            download_parallels=1,
            parallel_threshold_mb=500,
            auto_install=False,
        )
        cases = [
            ("needs-download", False, "download-started", 1),
            ("needs-verify", True, "verify-started", 1),
            ("verified", None, "verified", 0),
            ("active", None, "active", 0),
        ]
        for state, expected_flag, expected_state, launches in cases:
            launched.clear()
            result = {
                "state": state,
                "verification": "none",
                "model_id": spec.model_id,
                "local_dir": str(spec.local_dir),
                "percent": 0.0,
                "actual": 0,
                "expected": 1,
                "pid": None,
            }
            with (
                mock.patch.object(auto, "resolve_models", return_value=[spec]),
                mock.patch.object(auto, "inspect_model", return_value=result),
                mock.patch.object(auto, "launch_worker", side_effect=fake_launch),
                mock.patch.object(auto, "print_status"),
            ):
                rc = auto.command_ensure(args)
            self.assertEqual(rc, 0)
            self.assertEqual(len(launched), launches)
            if launches:
                self.assertEqual(launched[0], expected_flag)
            self.assertEqual(result["state"], expected_state)

    def test_command_ensure_does_not_relaunch_failed_verify(self) -> None:
        spec = auto.ModelSpec("org/name", Path("/tmp/org/name"))
        result = {
            "state": "needs-verify",
            "verification": "failed",
            "model_id": spec.model_id,
            "local_dir": str(spec.local_dir),
            "percent": 100.0,
            "actual": 1,
            "expected": 1,
            "pid": None,
        }
        with (
            mock.patch.object(auto, "resolve_models", return_value=[spec]),
            mock.patch.object(auto, "inspect_model", return_value=result),
            mock.patch.object(auto, "launch_worker") as launch,
            mock.patch.object(auto, "print_status"),
        ):
            rc = auto.command_ensure(argparse.Namespace(model=[spec], root=None, revision="master"))
        self.assertEqual(rc, 0)
        launch.assert_not_called()
        self.assertEqual(result["state"], "verify-failed")


class Sha256DecisionTests(unittest.TestCase):
    def test_hashing_does_not_publish_success_when_file_changes_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = verify.ModelSpec("org/name", Path(tmp))
            path = spec.local_dir / "weights.bin"
            path.write_bytes(b"abc")
            original = verify.sha256_file
            def race(path, chunk_size):
                digest = original(path, chunk_size)
                before = path.stat()
                path.write_bytes(b"abd")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                return digest
            with mock.patch.object(verify, "sha256_file", side_effect=race):
                checks = write_report(spec, [OFFICIAL[1]])
            self.assertEqual(checks[0].status, "changed_during_verification")
            self.assertFalse(json.loads((spec.local_dir / "modelscope_sha256.report.json").read_text(encoding="utf-8"))["all_ok"])

    def test_verify_model_classifies_missing_size_hash_and_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = verify.ModelSpec("org/name", Path(tmp))
            (spec.local_dir / "config.json").write_bytes(b"hello")
            (spec.local_dir / "weights.bin").write_bytes(b"XXXX")
            (spec.local_dir / "stray.txt").write_text("nope", encoding="utf-8")
            official = [
                {
                    "Type": "blob",
                    "Path": "config.json",
                    "Size": 5,
                    "Sha256": hashlib.sha256(b"hello").hexdigest(),
                },
                {
                    "Type": "blob",
                    "Path": "weights.bin",
                    "Size": 3,
                    "Sha256": hashlib.sha256(b"abc").hexdigest(),
                },
                {
                    "Type": "blob",
                    "Path": "missing.bin",
                    "Size": 1,
                    "Sha256": "ab",
                },
            ]
            with mock.patch.object(verify, "fetch_official_files", return_value=official):
                checks, summary = verify.verify_model(
                    spec, "master", 4096, ignore_extra=set(), ignore_official=set()
                )
            by_path = {item.path: item.status for item in checks}
            self.assertEqual(by_path["config.json"], "ok")
            self.assertEqual(by_path["weights.bin"], "size_mismatch")
            self.assertEqual(by_path["missing.bin"], "missing")
            self.assertEqual(summary["ok"], 1)
            self.assertEqual(summary["size_mismatch"], 1)
            self.assertEqual(summary["missing"], 1)
            self.assertIn("stray.txt", summary["extra_files"])

            (spec.local_dir / "weights.bin").write_bytes(b"abc")
            with mock.patch.object(verify, "fetch_official_files", return_value=official[:2]):
                checks, summary = verify.verify_model(
                    spec, "master", 4096, ignore_extra={"stray.txt"}, ignore_official=set()
                )
            self.assertTrue(all(item.status == "ok" for item in checks))
            self.assertEqual(summary["extra_file_count"], 0)

            (spec.local_dir / "weights.bin").write_bytes(b"abd")
            with mock.patch.object(verify, "fetch_official_files", return_value=official[:2]):
                checks, _summary = verify.verify_model(
                    spec, "master", 4096, ignore_extra={"stray.txt"}, ignore_official=set()
                )
            self.assertEqual(
                {item.path: item.status for item in checks}["weights.bin"],
                "sha256_mismatch",
            )


if __name__ == "__main__":
    unittest.main()
