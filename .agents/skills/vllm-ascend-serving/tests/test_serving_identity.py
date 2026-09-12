#!/usr/bin/env python3
"""Focused serving business-wrapper checks. No remote mutation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
LIB = ROOT / ".agents" / "lib"
for path in (str(SCRIPTS), str(LIB)):
    if path not in sys.path:
        sys.path.insert(0, path)

import _serving_start as serve_start
import _serving_status as serve_status
import _serving_stop as serve_stop
from vaws_task_target import reject_reserved_env, TaskTargetError


class ServingPresetTests(unittest.TestCase):
    def test_classify_stage_maps_runtime_log_markers(self):
        self.assertEqual(serve_start.classify_stage("Loading weights into memory"), "weight-load")
        self.assertEqual(serve_start.classify_stage("Uvicorn running on http://0.0.0.0:30001"), "http-up")
        self.assertIsNone(serve_start.classify_stage("unrelated log line"))

    def test_shipped_dsv4_flash_preset_preserves_managed_sources(self):
        preset = serve_start.load_preset("dsv4-flash")
        self.assertNotIn("vllm_version", preset)
        self.assertNotIn("PYTHONPATH", preset["env"])
        self.assertNotIn("VLLM_VERSION", preset["env"])
        self.assertEqual(preset["tp"], 8)
        self.assertIn("--quantization", preset["serve_args"])


class ServeCommandTests(unittest.TestCase):
    def test_sources_override_applies_only_to_the_submitted_execution(self):
        for sources in (None, {}, {"vllm": "/src/实验 with space"}):
            client = SimpleNamespace(run=mock.Mock(return_value={"state": "queued", "execution_id": "exec-one"}))
            args = ["--model", "/data/m", "--no-wait"]
            if sources is not None:
                args.extend(["--sources", json.dumps(sources)])
            with self.subTest(sources=sources), \
                 mock.patch.object(serve_start, "task_client", return_value=client), \
                 mock.patch.object(serve_start, "task_id_of", return_value="task-one"), \
                 mock.patch.object(serve_start, "load_serving_state", return_value=None), \
                 mock.patch.object(serve_start, "save_serving_state"), \
                 mock.patch.object(serve_start, "print_json"):
                self.assertEqual(serve_start.main(args), 0)
            self.assertEqual(client.run.call_args.kwargs["sources"], sources)

    def test_local_wrapper_and_execution_runtime_survive_the_business_receipt(self):
        client = SimpleNamespace(run=mock.Mock(return_value={"state": "queued", "execution_id": "exec-one"}))
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "wrapper.sh"
            wrapper.write_text("#!/bin/bash\nexec bash \"$1\"\n", encoding="utf-8")
            with mock.patch.object(serve_start, "task_client", return_value=client), \
                 mock.patch.object(serve_start, "task_id_of", return_value="task-one"), \
                 mock.patch.object(serve_start, "load_serving_state", return_value=None), \
                 mock.patch.object(serve_start, "save_serving_state") as save, \
                 mock.patch.object(serve_start, "print_json") as printed:
                self.assertEqual(serve_start.main(["--model", "/data/m", "--no-wait", "--wrap-script-local", str(wrapper)]), 0)
            report = save.call_args.args[1]
            self.assertEqual(report["execution_id"], "exec-one")
            self.assertEqual(report["wrap_script_content"], wrapper.read_text(encoding="utf-8"))
            self.assertIn(report["runtime_dir"], client.run.call_args.args[0])
            self.assertEqual(printed.call_args.args[0]["runtime_dir"], report["runtime_dir"])

    def test_nonpositive_device_requests_fail_before_task_or_run(self):
        for flag in ("--npu-count", "--tp", "--dp"):
            with self.subTest(flag=flag), mock.patch.object(serve_start, "task_client") as client, mock.patch.object(serve_start, "print_json"):
                self.assertEqual(serve_start.main(["--model", "/data/m", flag, "0"]), 1)
            client.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Bash execution fixture runs on Linux")
    def test_inline_wrapper_executes_the_same_quoted_serve_command(self):
        with tempfile.TemporaryDirectory(prefix="serving wrapper ") as tmp:
            runtime = Path(tmp) / "run with space"
            fake = Path(tmp) / "selected python"
            fake.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            fake.chmod(0o700)
            command = serve_start.build_serve_command(model="/models/a 'quoted' model", served_model_name="one",
                tp=1, dp=None, extra_args=["--enforce-eager"], runtime_dir=str(runtime),
                wrap_script_content='#!/bin/bash\nprintf wrapped > "$2/proof"\nexec bash "$1"\n')
            result = subprocess.run(["bash", "-c", command], text=True, capture_output=True,
                env={**os.environ, "VAWS_PYTHON": str(fake), "VAWS_SERVICE_PORT": "8000"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((runtime / "proof").read_text(), "wrapped")
            self.assertIn("/models/a 'quoted' model", result.stdout.splitlines())
            self.assertIn("--enforce-eager", result.stdout.splitlines())

    def test_command_uses_quoted_vaws_python_and_port(self):
        command = serve_start.build_serve_command(
            model="/data/model",
            served_model_name="m",
            tp=2,
            dp=None,
            extra_args=[],
        )
        self.assertIn('"$VAWS_PYTHON"', command)
        self.assertIn('"$VAWS_SERVICE_PORT"', command)
        self.assertIn("VAWS_PYTHON is unset", command)
        self.assertNotIn("python3 fallback", command)
        self.assertNotIn("${VAWS_PYTHON:-python3}", command)
        self.assertNotIn("getent hosts", command)
        self.assertNotIn("/etc/hosts", command)

    def test_preset_version_check_uses_selected_interpreter(self):
        command = serve_start.build_serve_command(
            model="/data/model",
            served_model_name="m",
            tp=1,
            dp=None,
            extra_args=[],
            expected_vllm="0.26.0",
        )
        self.assertIn('"$VAWS_PYTHON" -c', command)
        self.assertIn("0.26.0", command)

    def test_reserved_extra_env_is_rejected(self):
        with self.assertRaises(TaskTargetError):
            reject_reserved_env({"VAWS_SERVICE_PORT": "8000", "OK": "1"})


class QueuedStatusTests(unittest.TestCase):
    def test_rejected_start_keeps_previous_business_settings(self):
        client = SimpleNamespace(run=mock.Mock(side_effect=ValueError("service has different options")))
        with mock.patch.object(serve_start, "task_client", return_value=client), mock.patch.object(serve_start, "task_id_of", return_value="task-1"), mock.patch.object(serve_start, "load_serving_state", return_value=None), mock.patch.object(serve_start, "save_serving_state") as save, mock.patch.object(serve_start, "print_json"):
            rc = serve_start.main(["--model", "/data/m", "--service", "one"])
        self.assertEqual(rc, 2)
        save.assert_not_called()

    def test_terminal_start_reports_the_import_cause_without_http_probe(self):
        client = SimpleNamespace(
            run=mock.Mock(return_value={"state": "failed", "execution_id": "one", "resources_released": True}),
            observe=mock.Mock(return_value={"stdout": "ModuleNotFoundError: No module named 'vllm.example'",
                                           "stderr": "RuntimeError: Engine core initialization failed"}),
        )
        with mock.patch.object(serve_start, "task_client", return_value=client), mock.patch.object(serve_start, "task_id_of", return_value="task-1"), mock.patch.object(serve_start, "load_serving_state", return_value=None), mock.patch.object(serve_start, "save_serving_state"), mock.patch.object(serve_start, "wait_for_ready") as probe, mock.patch.object(serve_start, "print_json") as printed:
            rc = serve_start.main(["--model", "/data/m"])
        self.assertEqual(rc, 1)
        self.assertIn("No module named 'vllm.example'", printed.call_args.args[0]["error"])
        self.assertTrue(printed.call_args.args[0]["resources_released"])
        client.observe.assert_called_once_with("one", "tail")
        probe.assert_not_called()

    def test_launch_wait_follows_the_same_execution_until_running(self):
        client = SimpleNamespace(wait=mock.Mock(side_effect=[
            {"state": "preparing", "execution_id": "one", "wait_timed_out": True},
            {"state": "running", "execution_id": "one", "service_port": 8001},
        ]))
        with mock.patch.object(serve_start.time, "monotonic", return_value=10):
            result = serve_start.wait_for_launch(client, {"state": "queued", "execution_id": "one"}, 30)
        self.assertEqual(result["state"], "running")
        self.assertEqual(client.wait.call_count, 2)
        self.assertTrue(all(call.args == ("one",) for call in client.wait.call_args_list))

    def test_launch_wait_returns_terminal_failure_or_bounded_pending(self):
        client = SimpleNamespace(wait=mock.Mock(return_value={"state": "failed", "execution_id": "one"}))
        with mock.patch.object(serve_start.time, "monotonic", return_value=10):
            failed = serve_start.wait_for_launch(client, {"state": "preparing", "execution_id": "one"}, 30)
        self.assertEqual(failed["state"], "failed")
        client.wait.reset_mock()
        with mock.patch.object(serve_start.time, "monotonic", return_value=30):
            pending = serve_start.wait_for_launch(client, {"state": "queued", "execution_id": "one"}, 30)
        self.assertTrue(pending["wait_timed_out"])
        self.assertEqual(pending["execution_id"], "one")
        client.wait.assert_not_called()

    def test_start_preserves_pending_phase_without_port_probe(self):
        captured: dict = {}

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return {"state": "waiting_for_runtime", "execution_id": "abc"}

        client = SimpleNamespace(
            context={"session": {"id": "task-1"}},
            run=fake_run,
            observe=mock.Mock(side_effect=AssertionError("must not probe queued work")),
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(serve_start, "task_client", return_value=client), mock.patch.object(serve_start, "task_id_of", return_value="task-1"), mock.patch.object(serve_start, "load_serving_state", return_value=None), mock.patch.object(serve_start, "save_serving_state"), mock.patch.object(serve_start, "print_json") as printed:
            rc = serve_start.main(["--model", "/data/m", "--no-wait"])
        self.assertEqual(rc, 0)
        payload = printed.call_args[0][0]
        self.assertEqual(payload["status"], "waiting_for_runtime")
        self.assertFalse(payload["running"])
        client.observe.assert_not_called()
        self.assertEqual(captured["resources"]["service_port"], 0)
        self.assertEqual(captured["resources"]["npu_count"], 1)
        self.assertFalse(captured["restart"])
        self.assertNotIn("npu_count", captured)
        self.assertNotIn("devices", captured)
        self.assertNotIn("service_port", captured)

    def test_relaunch_submits_restart_true(self):
        captured: dict = {}

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return {"state": "waiting_for_runtime", "execution_id": "abc"}

        client = SimpleNamespace(
            context={"session": {"id": "task-1"}},
            run=fake_run,
            observe=mock.Mock(side_effect=AssertionError("must not probe queued work")),
        )
        previous = {
            "model": "/data/m",
            "served_model_name": "m",
            "tp": 2,
            "dp": 1,
            "env": {},
            "extra_args": [],
        }
        with mock.patch.object(serve_start, "task_client", return_value=client), mock.patch.object(serve_start, "task_id_of", return_value="task-1"), mock.patch.object(serve_start, "load_serving_state", return_value=previous), mock.patch.object(serve_start, "save_serving_state"), mock.patch.object(serve_start, "print_json"):
            rc = serve_start.main(["--relaunch", "--no-wait"])
        self.assertEqual(rc, 0)
        self.assertTrue(captured["restart"])
        self.assertEqual(captured["resources"]["npu_count"], 2)

    def test_status_preserves_pending_phase(self):
        client = SimpleNamespace(
            context={"session": {"id": "task-1"}},
            observe=lambda *a, **k: {"state": "queued", "execution_id": "abc"},
        )
        with mock.patch.object(serve_status, "task_client", return_value=client), mock.patch.object(serve_status, "task_id_of", return_value="task-1"), mock.patch.object(serve_status, "pick_execution", return_value={"state": "waiting_for_runtime", "execution_id": "abc"}), mock.patch.object(serve_status, "print_json") as printed:
            rc = serve_status.main([])
        self.assertEqual(rc, 0)
        payload = printed.call_args[0][0]
        self.assertEqual(payload["status"], "waiting_for_runtime")
        self.assertFalse(payload["running"])

    def test_preparing_keeps_the_same_phase_and_execution(self):
        from vaws_task_target import PENDING

        self.assertIn("preparing", PENDING)

        def fake_run(*_args, **kwargs):
            return {"state": "preparing", "execution_id": "exec-prep"}

        client = SimpleNamespace(
            context={"session": {"id": "task-1"}},
            run=fake_run,
            observe=mock.Mock(side_effect=AssertionError("must not probe preparing work")),
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(serve_start, "task_client", return_value=client), mock.patch.object(serve_start, "task_id_of", return_value="task-1"), mock.patch.object(serve_start, "load_serving_state", return_value=None), mock.patch.object(serve_start, "save_serving_state"), mock.patch.object(serve_start, "print_json") as printed:
            rc = serve_start.main(["--model", "/data/m", "--no-wait"])
        self.assertEqual(rc, 0)
        payload = printed.call_args[0][0]
        self.assertEqual(payload["status"], "preparing")
        self.assertEqual(payload["execution_id"], "exec-prep")
        self.assertEqual(payload["state"], "preparing")
        self.assertFalse(payload["running"])
        client.observe.assert_not_called()

        status_client = SimpleNamespace(
            context={"session": {"id": "task-1"}},
            observe=lambda *a, **k: {"state": "preparing", "execution_id": "exec-prep"},
        )
        with mock.patch.object(serve_status, "task_client", return_value=status_client), mock.patch.object(serve_status, "task_id_of", return_value="task-1"), mock.patch.object(serve_status, "pick_execution", return_value={"state": "preparing", "execution_id": "exec-prep"}), mock.patch.object(serve_status, "print_json") as printed:
            rc = serve_status.main([])
        self.assertEqual(rc, 0)
        status_payload = printed.call_args[0][0]
        self.assertEqual(status_payload["status"], "preparing")
        self.assertEqual(status_payload["execution_id"], "exec-prep")
        self.assertEqual(status_payload["state"], "preparing")
        self.assertFalse(status_payload["running"])


class NamedLookupTests(unittest.TestCase):
    def test_status_does_not_guess_an_unrelated_live_execution(self) -> None:
        client = SimpleNamespace(
            context={"session": {"id": "task-1"}},
            observe=mock.Mock(return_value={"state": "not_found", "service": "vllm"}),
        )
        observation = serve_status.pick_execution(client, "vllm", None)
        self.assertIsNone(observation)
        client.observe.assert_called_once_with(service="vllm")

    def test_stop_does_not_guess_an_unrelated_live_execution(self) -> None:
        client = SimpleNamespace(
            context={"session": {"id": "task-1"}},
            resolve_execution=mock.Mock(return_value=None),
        )
        self.assertIsNone(serve_stop.pick_id(client, "vllm", None))
        client.resolve_execution.assert_called_once_with(None, service="vllm")

    def test_environment_without_recipe_still_forwards_constraints(self) -> None:
        from vaws_task_target import named_environment

        env = named_environment(python_abi="cp311", soc="ascend910b", preset={"environment": {"machine_type": "A3"}})
        self.assertEqual(env["python_abi"], "cp311")
        self.assertEqual(env["soc"], "ascend910b")
        self.assertEqual(env["machine_type"], "A3")
        self.assertNotIn("recipe", env)


if __name__ == "__main__":
    unittest.main()
