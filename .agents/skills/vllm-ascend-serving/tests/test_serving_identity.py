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
import _common
import serve_start
import serve_stop
import serve_status

A3_DEVICES = """
| 0     Ascend910  | OK | 170 | 48 | 0 / 0 |
| 0     0         | 0000:9D:00.0 | 0 | 0 / 0 | 3364 / 65536 |
| 0     Ascend910  | OK | -   | 50 | 0 / 0 |
| 1     1         | 0000:9F:00.0 | 0 | 0 / 0 | 2951 / 65536 |
| 2     Ascend910  | OK | 170 | 48 | 0 / 0 |
| 0     8         | 0000:89:00.0 | 0 | 0 / 0 | 3000 / 65536 |
"""
A3_HEADER = '| NPU Chip | Process id | Process name | Process memory(MB) |\n'
A3_ROWS = '| 0 0 | 4321 | python3 | 123 |\n| 0 1 | 4322 | python3 | 122 |\n| 2 0 | 4323 | worker | 120 |\n'


class ServingNpuProbeTests(unittest.TestCase):
    def probe(self, output):
        with mock.patch.object(_common, "ssh_exec", return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")):
            return _common.probe_npus(object())

    def test_a3_process_rows_map_through_phy_id_below_hbm_threshold(self):
        parsed = self.probe(A3_DEVICES + A3_HEADER + A3_ROWS)
        self.assertEqual({key: value[0]["pid"] for key, value in parsed["busy"].items()},
                         {"0": 4321, "1": 4322, "8": 4323})
        self.assertEqual(parsed["free"], [])
        self.assertEqual(parsed["total"], 3)
        self.assertEqual(parsed["free_count"], 0)

    def test_missing_or_unparsable_process_table_fails_closed(self):
        for bad in (A3_DEVICES, A3_DEVICES + A3_HEADER + '| 0 9 | 4321 | python3 | 123 |\n'):
            with self.subTest(output=bad), self.assertRaisesRegex(RuntimeError, "parse failed"):
                self.probe(bad)


class ServingPresetTests(unittest.TestCase):
    def test_classify_stage_maps_runtime_log_markers(self):
        cases = {
            "Loading weights into memory": "weight-load",
            "Capturing ACL graphs for decode sizes": "graph-capture",
            "torch.compile init done": "compile",
            "Uvicorn running on http://0.0.0.0:30001": "http-up",
            "unrelated log line": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(serve_start.classify_stage(text), expected)

    def test_shipped_dsv4_flash_preset_is_valid_and_pinned(self):
        preset = serve_start.load_preset("dsv4-flash")
        self.assertEqual(preset["vllm_version"], "0.26.0")
        self.assertEqual(preset["tp"], 8)
        self.assertIn("--quantization", preset["serve_args"])
        import json as _json
        for flag in serve_start._JSON_VALUE_FLAGS:
            if flag in preset["serve_args"]:
                idx = preset["serve_args"].index(flag)
                _json.loads(preset["serve_args"][idx + 1])

    def preflight(self, preset, *, grep_rc=0, grep_out="", missing=(), args=None):
        def fake_ssh(ep, script, *, check=True):
            if script.startswith("grep -m1 '^__version__ = '"):
                return SimpleNamespace(returncode=grep_rc, stdout=grep_out, stderr="")
            if script.startswith("test -d "):
                path = script[len("test -d "):].strip("'\"")
                return SimpleNamespace(returncode=1 if path in missing else 0, stdout="", stderr="")
            raise AssertionError(f"unexpected ssh_exec: {script}")
        with mock.patch.object(serve_start, "ssh_exec", side_effect=fake_ssh):
            return serve_start.preflight_preset(
                object(), preset, runtime_base="/vllm-workspace",
                env={"PYTHONPATH": "/a:/b"}, extra_args=args or [],
            )

    def test_preflight_version_mismatch_blocks_launch(self):
        problems = self.preflight(
            {"vllm_version": "0.26.0"},
            grep_out="__version__ = version = '0.27.1'\n",
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("0.26.0", problems[0])
        self.assertIn("0.27.1", problems[0])

    def test_preflight_missing_pythonpath_and_bad_json(self):
        problems = self.preflight(
            {},
            missing=("/b",),
            args=["--additional-config", "{not json}"],
        )
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("/b" in p for p in problems))
        self.assertTrue(any("--additional-config" in p for p in problems))

    def test_preflight_passes_clean_recipe(self):
        self.assertEqual(self.preflight(
            {"vllm_version": "0.26.0"},
            grep_out="__version__ = version = '0.26.0'\n",
            args=["--additional-config", '{"a": true}'],
        ), [])


class ServingReadinessTests(unittest.TestCase):
    def run_wait(self, probes, token_results, timeout=25):
        state = {"t": 0.0}
        with mock.patch.object(serve_start.time, "monotonic", side_effect=lambda: state["t"]), \
             mock.patch.object(serve_start.time, "sleep", side_effect=lambda _: state.__setitem__("t", state["t"] + 10)), \
             mock.patch.object(serve_start, "probe_ready_once", side_effect=probes), \
             mock.patch.object(serve_start, "probe_first_token", side_effect=token_results), \
             mock.patch.object(serve_start, "read_remote_tail", return_value=""), \
             mock.patch.object(serve_start, "check_alive", return_value=True):
            return serve_start.wait_for_ready(object(), 1, 8000, "/rd", timeout, "model-x")

    def good_probe(self, **kw):
        probe = {"alive": True, "health": True, "models": {"data": [{"id": "model-x"}]},
                 "stage": None, "probe_error": False}
        probe.update(kw)
        return probe

    def test_ready_requires_first_token_and_records_phases(self):
        result = self.run_wait(
            [self.good_probe(stage="weight-load", health=False, models=None),
             self.good_probe(stage="graph-capture")],
            [{"ok": True, "probe_error": False, "detail": ""}],
        )
        self.assertTrue(result["ready"])
        names = [p["phase"] for p in result["phases"]]
        self.assertEqual(names, ["weight-load", "graph-capture", "health-ok", "models-ok", "first-token-ok"])

    def test_ssh_probe_error_is_not_a_dead_process(self):
        result = self.run_wait(
            [{"alive": False, "health": False, "models": None, "stage": None, "probe_error": True},
             self.good_probe()],
            [{"ok": True, "probe_error": False, "detail": ""}],
        )
        self.assertTrue(result["ready"])

    def test_first_token_failure_times_out_with_phase_hint(self):
        result = self.run_wait(
            [self.good_probe()] * 5,
            [{"ok": False, "probe_error": False, "detail": "500"}] * 5,
        )
        self.assertFalse(result["ready"])
        self.assertIn("first-token-failing", result["error"])
        self.assertEqual(result["last_phase"], "first-token-failing")


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
