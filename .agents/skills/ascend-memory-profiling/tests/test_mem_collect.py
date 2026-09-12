#!/usr/bin/env python3
"""Regression tests for mem_collect request quoting."""

from __future__ import annotations

import argparse
import importlib.util
import json
import io
import tempfile
from contextlib import ExitStack, redirect_stdout
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "ascend-memory-profiling" / "scripts"
LIB = ROOT / ".agents" / "lib"
for path in (str(SCRIPTS), str(LIB)):
    if path not in sys.path:
        sys.path.insert(0, path)


def load_module():
    sys.modules.pop("_common", None)
    spec = importlib.util.spec_from_file_location("_mem_collect_test", SCRIPTS / "mem_collect.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mem_collect = load_module()
EP = mem_collect.SshEndpoint(host="192.0.2.10", port=46001, user="root")


class SendInferenceQuotingTests(unittest.TestCase):
    def test_json_payload_is_shell_quoted(self) -> None:
        args = argparse.Namespace(image_url=None, prompt="it's a test", max_tokens=16, model="model", port=None)
        captured: list[str] = []

        def fake_ssh_exec(ep, script, **kwargs):
            captured.append(script)
            return subprocess.CompletedProcess([], 0, "{}", "")

        with mock.patch.object(mem_collect, "ssh_exec", side_effect=fake_ssh_exec):
            mem_collect.send_inference(EP, args, port=8000)

        self.assertEqual(len(captured), 1)
        cmd = captured[0]
        import shlex

        expected_payload = json.dumps(
            {"model": "model", "prompt": "it's a test", "max_tokens": 16, "temperature": 0.7}
        )
        self.assertIn(f"-d {shlex.quote(expected_payload)}", cmd)


class CollectionLifecycleTests(unittest.TestCase):
    def args(self):
        with mock.patch.object(sys, "argv", ["mem_collect.py", "--model", "/model with space", "--tp", "2",
                "--gpu-memory-utilization", "0.63", "--max-model-len", "8192", "--enforce-eager",
                "--speculative-config", '{"method": "mtp"}', "--enable-expert-parallel"]):
            return mem_collect.parse_args()

    def test_standalone_carries_wrapper_and_real_config_and_requires_csv_evidence(self):
        for csvs, code in (({"memory.csv": "msprof_csvs/memory.csv"}, 0), ({"__prof_device_map__": {}}, 1)):
            with self.subTest(csvs=csvs), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                run_dir = Path(tmp)
                start = {"status": "ready", "execution_id": "exec-1", "runtime_dir": "/tmp/vaws-serve.actual",
                         "port": 8000, "served_model_name": "model-name"}
                client = mock.Mock()
                client.observe.return_value = {"state": "cancelled", "resources_released": True, "stdout": "loaded"}
                target = {"task_id": "task-1", "execution_id": "exec-1", "endpoint": EP, "python": "/env/python",
                          "client": client, "target": {"devices": [3, 4]}}
                stack.enter_context(mock.patch("vaws_task_target.task_client", return_value=client))
                stack.enter_context(mock.patch.object(mem_collect, "ensure_run_dir", return_value=run_dir))
                launch = stack.enter_context(mock.patch.object(mem_collect.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(start), "")))
                stack.enter_context(mock.patch.object(mem_collect, "resolve_execution_target", return_value=target))
                for name in ("collect_npu_smi", "send_inference", "collect_model_config", "collect_weight_manifest"):
                    stack.enter_context(mock.patch.object(mem_collect, name, return_value={}))
                export = stack.enter_context(mock.patch.object(mem_collect, "run_msprof_export", return_value=["PROF_1"]))
                collect = stack.enter_context(mock.patch.object(mem_collect, "collect_msprof_csvs", return_value=csvs))
                stack.enter_context(redirect_stdout(io.StringIO()))
                self.assertEqual(mem_collect._main_standalone(self.args()), code)
                argv = launch.call_args.args[0]
                self.assertEqual(argv.count("start"), 1)
                self.assertEqual(argv[argv.index("--gpu-memory-utilization") + 1], "0.63")
                self.assertEqual(argv[argv.index("--max-model-len") + 1], "8192")
                self.assertEqual(argv[argv.index("--speculative-config") + 1], '{"method": "mtp"}')
                self.assertIn("--enforce-eager", argv)
                self.assertIn("--enable-expert-parallel", argv)
                wrapper = Path(argv[argv.index("--wrap-script-local") + 1])
                self.assertIn("# VAWS memory profiler wrapper", wrapper.read_text(encoding="utf-8"))
                export.assert_called_once_with(EP, "/tmp/vaws-serve.actual/msprof_data")
                collect.assert_called_once_with(EP, "/tmp/vaws-serve.actual", run_dir)
                client.observe.assert_any_call("exec-1", "stop", False)
                manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["component_data_available"], code == 0)
                self.assertEqual(manifest["baseline_source"], "unavailable")

    def test_target_resolution_failure_stops_the_execution_from_the_launch_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client = mock.Mock()
            client.observe.return_value = {"state": "cancelled", "resources_released": True}
            stack.enter_context(mock.patch("vaws_task_target.task_client", return_value=client))
            stack.enter_context(mock.patch.object(mem_collect, "ensure_run_dir", return_value=Path(tmp)))
            stack.enter_context(mock.patch.object(mem_collect.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 0, '{"status":"ready","execution_id":"exec-1"}', "")))
            stack.enter_context(mock.patch.object(mem_collect, "resolve_execution_target", side_effect=RuntimeError("missing endpoint")))
            with self.assertRaisesRegex(RuntimeError, "missing endpoint"):
                mem_collect._main_standalone(self.args())
            client.observe.assert_called_once_with("exec-1", "stop", False)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse(manifest["msprof_enabled"])

    def test_export_failure_is_reported_and_never_polls_unrelated_msprof_processes(self):
        common = sys.modules[mem_collect.run_msprof_export.__module__]
        with mock.patch.object(common, "ssh_write_text"), mock.patch.object(common, "ssh_exec", side_effect=[
                subprocess.CompletedProcess([], 0, "/tmp/profile/PROF_one\n", ""),
                subprocess.CompletedProcess([], 7, "", "export failed")]) as remote:
            with self.assertRaisesRegex(RuntimeError, "exit 7"):
                common.run_msprof_export(EP, "/tmp/profile data", timeout=120)
        command = remote.call_args.args[1]
        self.assertNotIn("pgrep", command)
        self.assertFalse(command.rstrip().endswith("&"))
        self.assertEqual(remote.call_args.kwargs["timeout"], 120)

    def test_attach_rejects_configuration_from_another_execution(self):
        args = argparse.Namespace(session_id="task-1", service="one")
        with mock.patch.object(mem_collect, "load_serving_state", return_value={"execution_id": "other", "model": "/wrong"}):
            with self.assertRaisesRegex(RuntimeError, "another execution"):
                mem_collect._resolve_attach_state(args, {"execution_id": "one", "live": True})


if __name__ == "__main__":
    unittest.main()
