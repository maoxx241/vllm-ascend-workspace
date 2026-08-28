#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
import tempfile
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from serve_start import service_runtime_dir  # noqa: E402
import serve_start
import serve_stop
import serve_status


class ServingIdentityTests(unittest.TestCase):
    def test_lost_ssh_is_not_a_dead_process_in_any_service_entrypoint(self):
        for module in (serve_start, serve_stop, serve_status):
            with self.subTest(module=module.__name__), mock.patch.object(module, "ssh_exec", return_value=SimpleNamespace(returncode=255, stdout="")):
                with self.assertRaisesRegex(RuntimeError, "unknown"):
                    module.check_alive(object(), 123)

    def test_stop_does_not_release_ports_on_unknown_process_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = SimpleNamespace(alias="test", endpoint=object(), session_id="session-test", state_repo_root=Path(tmp))
            with mock.patch.object(serve_stop, "resolve_execution_target", return_value=target), \
                 mock.patch.object(serve_stop, "file_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(serve_stop, "load_serving_state", return_value={"pid": 123, "port": 18000}), \
                 mock.patch.object(serve_stop, "ssh_exec", return_value=SimpleNamespace(returncode=255, stdout="")), \
                 mock.patch.object(serve_stop, "save_serving_state") as save, \
                 mock.patch.object(serve_stop, "release_service_port") as release, \
                 mock.patch.object(serve_stop, "print_json"):
                self.assertEqual(serve_stop.main(["--session-id", "session-test"]), 2)
                save.assert_not_called()
                release.assert_not_called()

    def test_source_only_staging_blocks_before_npu_probe_or_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = SimpleNamespace(record={}, alias="test", endpoint=object(), runtime_base="/workspace",
                                     session_id="session-test", session_file=None, session={}, state_repo_root=Path(tmp))
            with mock.patch.object(serve_start, "resolve_execution_target", return_value=target), \
                 mock.patch.object(serve_start, "file_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(serve_start, "require_session_npu_lease", return_value=[0]), \
                 mock.patch.object(serve_start, "load_serving_state", return_value=None), \
                 mock.patch.object(serve_start, "ssh_exec", return_value=SimpleNamespace(returncode=0)) as ssh, \
                 mock.patch.object(serve_start, "run_parity", return_value={"status": "source-only"}), \
                 mock.patch.object(serve_start, "probe_npus") as probe, \
                 mock.patch.object(serve_start, "print_json") as output:
                self.assertEqual(serve_start.main(["--model", "/models/test", "--tp", "1", "--devices", "0"]), 1)
                self.assertEqual(output.call_args.args[0]["status"], "blocked")
                probe.assert_not_called()
                self.assertEqual(ssh.call_count, 1)  # model-path existence only

    def test_missing_device_is_not_a_successful_free_probe(self):
        with mock.patch.object(serve_start, "probe_npus", return_value={"devices": [], "busy": {}}):
            self.assertFalse(serve_start.wait_for_devices_free(object(), {0}, timeout=0))

    def test_alias_namespaces_runtime_directory(self) -> None:
        self.assertEqual(
            service_runtime_dir("/vllm-workspace", "20260811_120000", "agent12345"),
            "/vllm-workspace/.vaws-runtime/serving/agent12345/20260811_120000",
        )

    def test_missing_alias_preserves_legacy_layout(self) -> None:
        self.assertEqual(
            service_runtime_dir("/vllm-workspace", "20260811_120000", None),
            "/vllm-workspace/.vaws-runtime/serving/20260811_120000",
        )


if __name__ == "__main__":
    unittest.main()
