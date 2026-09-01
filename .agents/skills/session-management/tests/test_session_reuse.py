#!/usr/bin/env python3
"""Regression tests for session_create --reuse-existing probe and rollback."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
MACHINE_SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"
SESSION_SCRIPT = (
    ROOT / ".agents" / "skills" / "session-management" / "scripts" / "session_create.py"
)
for path in (LIB, MACHINE_SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_module():
    spec = importlib.util.spec_from_file_location("_session_reuse_test", SESSION_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


session_create = load_module()

REUSE_SESSION = {
    "session_id": "sess-reuse",
    "status": "ready",
    "local": {"worktree_root": "/tmp/sess-reuse"},
    "remote": {
        "host": "192.0.2.10",
        "host_user": "root",
        "container": {"name": "vaws-sess-reuse", "ssh_port": 46001},
    },
}


def run_main(argv: list[str]) -> tuple[int, dict]:
    stdout = io.StringIO()
    with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
        returncode = session_create.main()
    return returncode, json.loads(stdout.getvalue())


class SessionReuseTests(unittest.TestCase):
    def reuse_lookup(self) -> SimpleNamespace:
        return SimpleNamespace(
            session=dict(REUSE_SESSION),
            session_file=Path("/tmp/sess-reuse/session.json"),
        )

    def test_reuse_reports_ready_only_when_probe_confirms_alive(self) -> None:
        with (
            mock.patch.object(session_create, "load_session_lookup", return_value=self.reuse_lookup()),
            mock.patch.object(
                session_create,
                "probe_container_alive",
                return_value={"alive": True, "reason": "container ssh reachable"},
            ),
        ):
            returncode, payload = run_main(
                ["session_create.py", "--machine", "machine-a", "--session-id", "sess-reuse", "--reuse-existing"]
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["reused"])

    def test_reuse_inconclusive_probe_is_needs_repair_not_ready(self) -> None:
        # probe_container_alive never returns alive=False; an unreachable or
        # timing-out SSH endpoint yields alive=None and must not surface the
        # stored "ready" status.
        for probe in (
            {"alive": None, "reason": "ssh probe timed out (inconclusive)"},
            {"alive": None, "reason": "container ssh unreachable; workload state unknown (rc=255)"},
        ):
            with (
                self.subTest(probe=probe),
                mock.patch.object(session_create, "load_session_lookup", return_value=self.reuse_lookup()),
                mock.patch.object(session_create, "probe_container_alive", return_value=probe),
            ):
                returncode, payload = run_main(
                    ["session_create.py", "--machine", "machine-a", "--session-id", "sess-reuse", "--reuse-existing"]
                )

            self.assertEqual(returncode, 1)
            self.assertEqual(payload["status"], "needs_repair")
            self.assertFalse(payload["reused"])

    def test_reuse_probe_exception_is_inconclusive_and_keeps_leases(self) -> None:
        with (
            mock.patch.object(session_create, "load_session_lookup", return_value=self.reuse_lookup()),
            mock.patch.object(
                session_create,
                "probe_container_alive",
                side_effect=OSError("local ssh binary missing"),
            ),
            mock.patch.object(session_create, "release_all_session_leases") as release,
        ):
            returncode, payload = run_main(
                ["session_create.py", "--machine", "machine-a", "--session-id", "sess-reuse", "--reuse-existing"]
            )

        self.assertEqual(returncode, 1)
        self.assertEqual(payload["status"], "needs_repair")
        self.assertIsNone(payload["container_probe"]["alive"])
        release.assert_not_called()

    def test_reuse_state_failure_never_releases_existing_leases(self) -> None:
        with (
            mock.patch.object(
                session_create,
                "load_session_lookup",
                side_effect=RuntimeError("corrupt session state"),
            ),
            mock.patch.object(session_create, "release_all_session_leases") as release,
        ):
            returncode, payload = run_main(
                ["session_create.py", "--machine", "machine-a", "--session-id", "sess-reuse", "--reuse-existing"]
            )

        self.assertEqual(returncode, 2)
        self.assertEqual(payload["status"], "failed")
        release.assert_not_called()

    def test_creation_failure_still_releases_newly_allocated_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            machine = {
                "alias": "machine-a",
                "namespace": "ns",
                "host": {"ip": "192.0.2.10", "user": "root", "port": 22},
                "container": {"image": "img", "workdir": "/vllm-workspace"},
            }
            with (
                mock.patch.object(session_create, "load_machine", return_value=machine),
                mock.patch.object(session_create, "probe_host_npu_devices", return_value=([0], {"status": "ok"})),
                mock.patch.object(session_create, "host_port_availability", return_value=lambda port: True),
                mock.patch.object(session_create, "ensure_worktree", return_value=(worktree, {"action": "created-branch"})),
                mock.patch.object(session_create, "write_current_session_binding", return_value=Path(tmp) / "binding.json"),
                mock.patch.object(
                    session_create,
                    "allocate_session_leases",
                    return_value={"container_ssh_port": 46001, "npu_devices": [0]},
                ),
                mock.patch.object(session_create, "save_session", return_value=Path(tmp) / "session.json"),
                mock.patch.object(
                    session_create,
                    "load_session_lookup",
                    return_value=SimpleNamespace(session={"session_id": "sess-new"}),
                ),
                mock.patch.object(
                    session_create,
                    "bootstrap_container",
                    side_effect=RuntimeError("docker daemon unreachable"),
                ),
                mock.patch.object(session_create, "release_all_session_leases") as release,
            ):
                returncode, payload = run_main(
                    ["session_create.py", "--machine", "machine-a", "--session-id", "sess-new"]
                )

        self.assertEqual(returncode, 2)
        self.assertEqual(payload["status"], "failed")
        release.assert_called_once_with(repo_root=session_create.ROOT, session_id="sess-new")


if __name__ == "__main__":
    unittest.main()
