"""Local tests for benchmark scripts; no SSH/NPU access (remote calls mocked)."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents/skills/vllm-ascend-benchmark/scripts"
PRESETS = ROOT / ".agents/skills/vllm-ascend-benchmark/presets"
FIXTURES = ROOT / ".agents/skills/vllm-ascend-benchmark/tests/fixtures"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _benchmark_common as _common  # noqa: E402
import bench_run  # noqa: E402

def _assemble(**kwargs):
    kwargs.pop("session_id", None)
    client = SimpleNamespace(context={"session": {"id": "task-1"}})
    with mock.patch.object(_common, "task_client", return_value=client), mock.patch.object(
        _common, "task_id_of", return_value="task-1"
    ):
        return _common.assemble_config(**kwargs)


class BenchmarkEntrypointSmokeTests(unittest.TestCase):
    def test_benchmark_clis_have_help(self):
        for script in sorted(SCRIPTS.glob("bench_*.py")):
            with self.subTest(script=script.name):
                proc = subprocess.run([sys.executable, str(script), "--help"],
                                      capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)


class BenchQueuedTests(unittest.TestCase):
    def test_queued_serve_is_not_force_stopped(self):
        cfg = SimpleNamespace(
            context_file=None, execution_id=None, service="vllm",
            task_id="task-1", model="/m", health_timeout=None,
        )
        stop = mock.Mock(side_effect=AssertionError("must not stop queued work"))
        with mock.patch.object(bench_run, "assemble_config", return_value=cfg), mock.patch.object(
            bench_run, "call_serve_start",
            return_value={"status": "queued", "execution_id": "exec-q", "state": "queued"},
        ), mock.patch.object(bench_run, "call_serve_stop", stop), mock.patch.object(
            bench_run, "print_json"
        ) as printed:
            rc = bench_run.main(["--model", "/m"])
        self.assertEqual(rc, 0)
        payload = printed.call_args[0][0]
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["execution_id"], "exec-q")
        stop.assert_not_called()

    def test_preparing_execution_is_not_force_stopped(self):
        from vaws_task_target import PENDING

        self.assertIn("preparing", PENDING)
        cfg = SimpleNamespace(
            context_file=None, execution_id="exec-prep", service="vllm",
            task_id="task-1", model="/m", health_timeout=30,
        )
        stop = mock.Mock(side_effect=AssertionError("must not stop preparing work"))
        client = SimpleNamespace(
            observe=lambda *a, **k: {"state": "preparing", "execution_id": "exec-prep"},
        )
        with mock.patch.object(bench_run, "assemble_config", return_value=cfg), mock.patch.object(
            bench_run, "call_serve_stop", stop
        ), mock.patch.object(bench_run, "print_json") as printed, mock.patch(
            "vaws_task_target.task_client", return_value=client
        ):
            rc = bench_run.main(["--execution-id", "exec-prep", "--model", "/m"])
        self.assertEqual(rc, 0)
        payload = printed.call_args[0][0]
        self.assertEqual(payload["status"], "preparing")
        self.assertEqual(payload["execution_id"], "exec-prep")
        self.assertEqual(payload["state"], "preparing")
        stop.assert_not_called()

    def test_supplied_live_service_is_not_stopped_on_error(self):
        cfg = SimpleNamespace(
            context_file=None, execution_id="exec-live", service="vllm",
            task_id="task-1", model="/m", health_timeout=30,
        )
        stop = mock.Mock(side_effect=AssertionError("must not stop a supplied service"))
        fake_serve = SimpleNamespace(
            wait_for_ready=lambda *a, **k: {"ready": False, "error": "health failed"},
        )
        live = {
            "state": "running", "live": True, "service_port": 8000,
            "endpoint": {"host": "10.0.0.1", "port": 22, "user": "root"},
        }
        client = SimpleNamespace(
            observe=lambda *a, **k: {"state": "running", "execution_id": "exec-live"},
        )
        with mock.patch.object(bench_run, "assemble_config", return_value=cfg), mock.patch.object(
            bench_run, "call_serve_stop", stop
        ), mock.patch.object(bench_run, "print_json") as printed, mock.patch(
            "vaws_task_target.task_client", return_value=client
        ), mock.patch(
            "vaws_task_target.execution_target", return_value=live
        ), mock.patch(
            "vaws_remote_target.ssh_endpoint_from_mapping",
            return_value=SimpleNamespace(host="10.0.0.1", port=22, user="root"),
        ), mock.patch.dict(sys.modules, {"_serving_start": fake_serve}):
            rc = bench_run.main(["--execution-id", "exec-live", "--model", "/m"])
        self.assertEqual(rc, 1)
        payload = printed.call_args[0][0]
        self.assertEqual(payload["status"], "incomplete")
        stop.assert_not_called()


class PresetTests(unittest.TestCase):
    def test_dsv4_flash_preset_file_has_required_keys(self):
        preset = json.loads((PRESETS / "dsv4-flash.json").read_text(encoding="utf-8"))
        required = {
            "tp", "dp", "port", "devices", "served_model_name", "health_timeout",
            "env", "bench_env", "serve_args", "bench_args",
        }
        self.assertLessEqual(required, set(preset))
        # Model weight paths are machine-specific; the preset must not pin one.
        self.assertNotIn("model", preset)

    def test_load_preset_by_bare_name_and_suffix(self):
        by_name = _common.load_preset("dsv4-flash")
        by_suffix = _common.load_preset("dsv4-flash.json")
        self.assertEqual(by_name, by_suffix)
        self.assertEqual(by_name["tp"], 8)

    def test_load_preset_unknown_and_traversal_rejected(self):
        for bad in ["no-such-preset", "../dsv4-flash", "/etc/passwd", "a/b", ".."]:
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    _common.load_preset(bad)


class AssembleConfigPresetTests(unittest.TestCase):
    def test_preset_values_flow_into_config(self):
        cfg = _assemble(preset="dsv4-flash", model="/m")
        self.assertEqual(cfg.tp, 8)
        self.assertEqual(cfg.dp, 1)
        self.assertEqual(cfg.port, 30001)
        self.assertEqual(cfg.devices, "0,1,2,3,4,5,6,7")
        self.assertEqual(cfg.served_model_name, "dsv4-w4a8")
        self.assertEqual(cfg.health_timeout, 1200)
        self.assertNotIn("PYTHONPATH", cfg.env)
        self.assertNotIn("PYTHONPATH", cfg.bench_env)
        self.assertIn("--enable-expert-parallel", cfg.serve_args)
        self.assertIn("--tokenizer-mode", cfg.serve_args)
        self.assertEqual(cfg.bench_args[cfg.bench_args.index("--dataset-name") + 1], "random")
        self.assertEqual(cfg.preset_name, "dsv4-flash")
        self.assertNotIn("vllm_ref", cfg.preset)

    def test_cli_overrides_beat_preset(self):
        cfg = _assemble(
            preset="dsv4-flash", model="/m",
            tp=4, dp=2, port=40000, devices="0,1", served_model_name="other",
            health_timeout=10,
            extra_env=["VLLM_VERSION=9.9"],
            bench_env=["PYTHONPATH=/x"],
            serve_args=["--max-model-len", "128"],
            bench_args=["--num-prompts", "2"],
        )
        self.assertEqual(cfg.tp, 4)
        self.assertEqual(cfg.dp, 2)
        self.assertEqual(cfg.port, 40000)
        self.assertEqual(cfg.devices, "0,1")
        self.assertEqual(cfg.served_model_name, "other")
        self.assertEqual(cfg.health_timeout, 10)
        self.assertEqual(cfg.env["VLLM_VERSION"], "9.9")
        self.assertEqual(cfg.bench_env["PYTHONPATH"], "/x")
        self.assertEqual(cfg.serve_args, ["--max-model-len", "128"])
        self.assertEqual(cfg.bench_args, ["--num-prompts", "2"])
        # Preset env keys not overridden still apply.
        self.assertEqual(cfg.env["HCCL_BUFFSIZE"], "1024")
        self.assertNotIn("VLLM_VERSION", cfg.bench_env)


class ServeStartArgsTests(unittest.TestCase):
    def test_new_flags_emitted_when_set(self):
        cfg = _common.BenchConfig(
             model="/m",
            served_model_name="dsv4-w4a8", devices="0,1", health_timeout=1200,
        )
        args = cfg.to_serve_start_args()
        self.assertEqual(args[args.index("--served-model-name") + 1], "dsv4-w4a8")
        self.assertEqual(args[args.index("--devices") + 1], "0,1")
        self.assertEqual(args[args.index("--health-timeout") + 1], "1200")

    def test_new_flags_omitted_when_unset(self):
        args = _common.BenchConfig( model="/m").to_serve_start_args()
        self.assertNotIn("--served-model-name", args)
        self.assertNotIn("--devices", args)
        self.assertNotIn("--health-timeout", args)


class BenchEnvExportTests(unittest.TestCase):
    def setUp(self):
        self.target = {"live": True, "python": "/managed/bin/python", "launch_preamble": "export MANAGED=1", "launch_observation": {}}
        for name, value in (("task_client", object()), ("execution_target", self.target)):
            patcher = mock.patch.object(_common, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_bench_env_exported_in_remote_script(self):
        cfg = _common.BenchConfig(
             model="/m", execution_id="owned",
            bench_env={"PYTHONPATH": "/a:/b", "VLLM_VERSION": "0.21.0"},
        )
        captured = {}

        def fake_ssh(endpoint, script, **kwargs):
            captured["script"] = script
            return subprocess.CompletedProcess(
                ["ssh"], 0, stdout='{"output_throughput": 1.0}', stderr="",
            )

        with mock.patch.object(_common, "ssh_exec", side_effect=fake_ssh):
            result = _common.run_bench_on_remote(
                cfg, "http://127.0.0.1:30001", "m", "10.0.0.1", 2222,
            )
        script = captured["script"]
        self.assertIn("/managed/bin/python -m vllm.entrypoints.cli.main", script)
        self.assertIn("export MANAGED=1", script)
        self.assertIn("export PYTHONPATH=", script)
        self.assertIn("export VLLM_VERSION=", script)
        self.assertEqual(result["output_throughput"], 1.0)

    def test_no_bench_env_means_no_exports(self):
        cfg = _common.BenchConfig(model="/m", execution_id="owned")
        captured = {}

        def fake_ssh(endpoint, script, **kwargs):
            captured["script"] = script
            return subprocess.CompletedProcess(
                ["ssh"], 0, stdout='{"output_throughput": 1.0}', stderr="",
            )

        with mock.patch.object(_common, "ssh_exec", side_effect=fake_ssh):
            _common.run_bench_on_remote(cfg, "http://127.0.0.1:30001", "m", "10.0.0.1", 2222)
        self.assertNotIn("export PYTHONPATH=", captured["script"])


class FixedDatasetArgsTests(unittest.TestCase):
    BASE = [
        "--dataset-name", "random", "--seed", "0",
        "--random-input-len", "512", "--random-output-len", "512",
        "--ignore-eos", "--num-prompts", "1", "--max-concurrency", "1",
    ]

    def test_fixed_dataset_switch(self):
        out = _common.fixed_dataset_bench_args(
            self.BASE, dataset_path="/tmp/ds.jsonl", output_len=512,
        )
        self.assertEqual(out[out.index("--dataset-name") + 1], "custom")
        self.assertEqual(out[out.index("--dataset-path") + 1], "/tmp/ds.jsonl")
        self.assertEqual(out[out.index("--custom-output-len") + 1], "512")
        self.assertIn("--skip-chat-template", out)
        self.assertIn("--disable-shuffle", out)
        self.assertIn("--ignore-eos", out)
        self.assertEqual(out.count("--ignore-eos"), 1)
        # Random-dataset flags are stripped; unrelated flags are preserved.
        self.assertNotIn("random", out)
        self.assertNotIn("--random-input-len", out)
        self.assertNotIn("--random-output-len", out)
        self.assertEqual(out[out.index("--seed") + 1], "0")
        self.assertEqual(out[out.index("--num-prompts") + 1], "1")





class RealBenchResultFixtureTests(unittest.TestCase):
    """End-to-end over a fixture shaped like real `vllm bench serve
    --save-result` output (see the fixture's `_source` note for provenance:
    vllm/vllm/benchmarks/serve.py:971-1062)."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(
            (FIXTURES / "vllm_bench_serve_result.json").read_text(encoding="utf-8")
        )

    def test_extract_metrics_aligns_with_real_output_keys(self):
        metrics = _common.extract_metrics(self.fixture)
        self.assertEqual(metrics["total_input_tokens"], 32768)
        self.assertEqual(metrics["total_output_tokens"], 32768)
        self.assertEqual(metrics["total_token_throughput"], 1070.24)
        self.assertEqual(metrics["output_throughput"], 535.12)
        self.assertEqual(metrics["request_throughput"], 1.0452)
        self.assertEqual(metrics["mean_tpot_ms"], 28.41)
        self.assertEqual(metrics["median_ttft_ms"], 118.21)
        self.assertEqual(metrics["p99_tpot_ms"], 31.27)
        self.assertEqual(metrics["mean_e2el_ms"], 14612.3)
        self.assertEqual(metrics["spec_decode_acceptance_rate"], 0.5702)
        # Keys that never existed in the real output are not extracted.
        self.assertNotIn("acceptance_rate", metrics)
        self.assertNotIn("total_input", metrics)
        self.assertNotIn("total_output", metrics)
        # Provenance and raw per-request arrays do not leak into metrics.
        self.assertNotIn("_source", metrics)
        self.assertNotIn("input_lens", metrics)



class StreamingTimeoutTests(unittest.TestCase):
    def test_run_json_command_streaming_times_out_and_kills(self):
        start = time.monotonic()
        returncode, payload, _stdout, stderr = _common._run_json_command_streaming(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1,
        )
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(returncode, 124)
        self.assertIsNone(payload)
        self.assertIn("timed out after", stderr)

    def test_run_json_command_streaming_without_timeout_unchanged(self):
        returncode, payload, _stdout, _stderr = _common._run_json_command_streaming(
            [sys.executable, "-c", "import json; print(json.dumps({'ok': 1}))"],
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(payload, {"ok": 1})

    def test_call_serve_start_bounds_subprocess_by_health_timeout(self):
        cfg = _common.BenchConfig( model="/m", health_timeout=1200)
        with mock.patch.object(
            _common, "_run_json_command_streaming",
            return_value=(0, {"status": "ready"}, '{"status": "ready"}', ""),
        ) as m:
            _common.call_serve_start(cfg)
        self.assertEqual(
            m.call_args.kwargs["timeout"],
            1200 + _common._SERVE_START_TIMEOUT_MARGIN,
        )

    def test_call_serve_start_timeout_falls_back_to_serving_default(self):
        cfg = _common.BenchConfig(model="/m", execution_id="owned")
        with mock.patch.object(
            _common, "_run_json_command_streaming",
            return_value=(0, {"status": "ready"}, '{"status": "ready"}', ""),
        ) as m:
            _common.call_serve_start(cfg)
        self.assertEqual(
            m.call_args.kwargs["timeout"],
            _common._SERVE_START_DEFAULT_HEALTH_TIMEOUT
            + _common._SERVE_START_TIMEOUT_MARGIN,
        )


class WarmupValidationTests(unittest.TestCase):
    def test_bench_run_rejects_warmup_ge_runs(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as cm:
            bench_run.main(["--model", "/m", "--runs", "2", "--warmup-runs", "2"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("must be >= 0 and less than --runs", stderr.getvalue())

    def test_bench_run_accepts_valid_warmup(self):
        # warmup < runs passes validation; the run then proceeds past argument
        # handling (assemble_config is mocked to stop before any remote work).
        try:
            with mock.patch.object(
                bench_run, "assemble_config", side_effect=RuntimeError("stop here")
            ), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = bench_run.main(["--model", "/m", "--runs", "2", "--warmup-runs", "1"])
        except SystemExit:
            self.fail("valid --warmup-runs must not trigger parser.error")
        self.assertEqual(rc, 2)





class RemoteHelperTests(unittest.TestCase):


    def test_prepare_fixed_request_dataset_parses_remote_json(self):
        payload = json.dumps({
            "status": "ok", "dataset_path": "/tmp/x.jsonl", "num_rows": 2,
            "prompt_token_len": 512, "output_len": 512, "prompt_sha256": "abc",
        })
        proc = subprocess.CompletedProcess([], 0, stdout=f"noise\n{payload}\n", stderr="")
        with mock.patch.object(_common, "ssh_run_script", return_value=proc) as m:
            out = _common.prepare_fixed_request_dataset(
                "10.0.0.1", 2222,
                model="/m", tokenizer_mode="auto", input_len=512, output_len=512,
                path="/tmp/x.jsonl", num_rows=2,
                env_preamble="export PYTHONPATH=/a; ",
            )
        self.assertEqual(out["prompt_sha256"], "abc")
        script = m.call_args[0][2]
        self.assertIn("VAWS_FIXED_INPUT_LEN=512", script)
        self.assertIn("VAWS_FIXED_NUM_ROWS=2", script)
        self.assertIn("export PYTHONPATH=/a;", script)

    def test_prepare_fixed_request_dataset_hard_failure_raises(self):
        proc = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="fixed prompt token length mismatch",
        )
        with mock.patch.object(_common, "ssh_run_script", return_value=proc):
            with self.assertRaises(RuntimeError):
                _common.prepare_fixed_request_dataset(
                    "10.0.0.1", 2222,
                    model="/m", tokenizer_mode="auto", input_len=512, output_len=512,
                    path="/tmp/x.jsonl", num_rows=1,
                )

    def test_accuracy_probe_http_error_does_not_raise(self):
        proc = subprocess.CompletedProcess(
            [], 0,
            stdout='{"status": "failed", "http_status": 500, "body": "boom"}\n',
            stderr="",
        )
        with mock.patch.object(_common, "ssh_run_script", return_value=proc):
            out = _common.run_accuracy_probe(
                "10.0.0.1", 2222, port=30001, model="m", prompt="hi", max_tokens=64,
            )
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["http_status"], 500)
        self.assertIn("prompt_sha256", out)

    def test_accuracy_probe_ok_returns_text_hash(self):
        proc = subprocess.CompletedProcess(
            [], 0,
            stdout='{"status": "ok", "text_sha256": "deadbeef", "text": "80234",'
                   ' "finish_reason": "stop", "usage": {}}\n',
            stderr="",
        )
        with mock.patch.object(_common, "ssh_run_script", return_value=proc) as m:
            out = _common.run_accuracy_probe(
                "10.0.0.1", 2222, port=30001, model="m", prompt="hi", max_tokens=64,
            )
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["text_sha256"], "deadbeef")
        self.assertIn("/v1/completions", m.call_args[0][2])





if __name__ == "__main__":
    unittest.main()
