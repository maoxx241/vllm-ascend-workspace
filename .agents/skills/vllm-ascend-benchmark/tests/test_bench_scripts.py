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

import _common  # noqa: E402
import bench_compare  # noqa: E402
import bench_run  # noqa: E402

FAKE_LOOKUP = SimpleNamespace(
    session={"session_id": "s"},
    session_file="/tmp/session.json",
)


def _assemble(**kwargs):
    """assemble_config with session resolution mocked out."""
    with mock.patch.object(_common, "load_session_lookup", return_value=FAKE_LOOKUP):
        return _common.assemble_config(**kwargs)


class BenchmarkEntrypointSmokeTests(unittest.TestCase):
    def test_benchmark_clis_have_help(self):
        for script in sorted(SCRIPTS.glob("bench_*.py")):
            with self.subTest(script=script.name):
                proc = subprocess.run([sys.executable, str(script), "--help"],
                                      capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)


class PresetTests(unittest.TestCase):
    def test_dsv4_flash_preset_file_has_required_keys(self):
        preset = json.loads((PRESETS / "dsv4-flash.json").read_text(encoding="utf-8"))
        required = {
            "tp", "dp", "port", "devices", "served_model_name", "health_timeout",
            "vllm_ref", "runs", "warmup_runs", "env", "bench_env",
            "serve_args", "bench_args", "fixed_request_dataset",
            "bench_request_counts",
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
        cfg = _assemble(preset="dsv4-flash", model="/m", session_id="s")
        self.assertEqual(cfg.tp, 8)
        self.assertEqual(cfg.dp, 1)
        self.assertEqual(cfg.port, 30001)
        self.assertEqual(cfg.devices, "0,1,2,3,4,5,6,7")
        self.assertEqual(cfg.served_model_name, "dsv4-w4a8")
        self.assertEqual(cfg.health_timeout, 1200)
        self.assertEqual(cfg.env["VLLM_VERSION"], "0.26.0")
        self.assertIn("/vllm-workspace/vllm", cfg.env["PYTHONPATH"])
        self.assertEqual(cfg.bench_env["VLLM_VERSION"], "0.26.0")
        self.assertIn("/vllm-workspace/vllm-ascend", cfg.bench_env["PYTHONPATH"])
        self.assertIn("--enable-expert-parallel", cfg.serve_args)
        self.assertIn("--tokenizer-mode", cfg.serve_args)
        self.assertEqual(cfg.bench_args[cfg.bench_args.index("--dataset-name") + 1], "random")
        self.assertEqual(cfg.preset_name, "dsv4-flash")
        # Non-config preset keys stay readable for callers.
        self.assertEqual(cfg.preset["vllm_ref"], "967c5c3bc38891f4465d3f4e99917ed837bb3833")
        self.assertEqual(cfg.preset["bench_request_counts"], [1])
        self.assertEqual(cfg.preset["runs"], 6)
        self.assertEqual(cfg.preset["warmup_runs"], 1)
        self.assertEqual(
            cfg.preset["fixed_request_dataset"]["path"],
            "/tmp/vaws_dsv4_fixed_requests_512x512.jsonl",
        )

    def test_cli_overrides_beat_preset(self):
        cfg = _assemble(
            preset="dsv4-flash", model="/m", session_id="s",
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
        self.assertEqual(cfg.bench_env["VLLM_VERSION"], "0.26.0")


class ServeStartArgsTests(unittest.TestCase):
    def test_new_flags_emitted_when_set(self):
        cfg = _common.BenchConfig(
            session_id="s", model="/m",
            served_model_name="dsv4-w4a8", devices="0,1", health_timeout=1200,
        )
        args = cfg.to_serve_start_args()
        self.assertEqual(args[args.index("--served-model-name") + 1], "dsv4-w4a8")
        self.assertEqual(args[args.index("--devices") + 1], "0,1")
        self.assertEqual(args[args.index("--health-timeout") + 1], "1200")

    def test_new_flags_omitted_when_unset(self):
        args = _common.BenchConfig(session_id="s", model="/m").to_serve_start_args()
        self.assertNotIn("--served-model-name", args)
        self.assertNotIn("--devices", args)
        self.assertNotIn("--health-timeout", args)


class BenchEnvExportTests(unittest.TestCase):
    def test_bench_env_exported_in_remote_script(self):
        cfg = _common.BenchConfig(
            session_id="s", model="/m",
            bench_env={"PYTHONPATH": "/a:/b", "VLLM_VERSION": "0.21.0"},
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"output_throughput": 1.0}', stderr="",
            )

        with mock.patch.object(_common.subprocess, "run", side_effect=fake_run):
            result = _common.run_bench_on_remote(
                cfg, "http://127.0.0.1:30001", "m", "10.0.0.1", 2222,
            )
        script = " ".join(captured["cmd"])
        self.assertIn("export PYTHONPATH=", script)
        self.assertIn("export VLLM_VERSION=", script)
        self.assertEqual(result["output_throughput"], 1.0)

    def test_no_bench_env_means_no_exports(self):
        cfg = _common.BenchConfig(session_id="s", model="/m")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"output_throughput": 1.0}', stderr="",
            )

        with mock.patch.object(_common.subprocess, "run", side_effect=fake_run):
            _common.run_bench_on_remote(cfg, "http://127.0.0.1:30001", "m", "10.0.0.1", 2222)
        self.assertNotIn("export PYTHONPATH=", " ".join(captured["cmd"]))


class FixedDatasetArgsTests(unittest.TestCase):
    BASE = [
        "--dataset-name", "random", "--seed", "0",
        "--random-input-len", "512", "--random-output-len", "512",
        "--ignore-eos", "--num-prompts", "1", "--max-concurrency", "1",
    ]

    def test_fixed_dataset_switch(self):
        out = bench_compare.fixed_dataset_bench_args(
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

    def test_request_count_overrides_num_prompts_and_concurrency(self):
        cases = bench_compare.build_cases(self.BASE, [None, 2])
        self.assertEqual(cases[0]["case"], "default")
        self.assertIsNone(cases[0]["request_count"])
        self.assertEqual(cases[1]["case"], "requests_2")
        self.assertEqual(cases[1]["request_count"], 2)
        args = cases[1]["bench_args"]
        self.assertEqual(args[args.index("--num-prompts") + 1], "2")
        self.assertEqual(args[args.index("--max-concurrency") + 1], "2")
        # The default case keeps the base args untouched.
        self.assertEqual(cases[0]["bench_args"], self.BASE)


class CompareTests(unittest.TestCase):
    def _state(self, label, default_tpot, case2_tpot):
        return {
            "label": label,
            "ref": label,
            "cases": [
                {
                    "case": "default",
                    "request_count": None,
                    "aggregated": {
                        "mean_tpot_ms": {"mean": default_tpot, "stddev": 0.1},
                        "output_throughput": {"mean": 100.0},
                        "spec_decode_acceptance_rate": {"mean": 0.5},
                    },
                },
                {
                    "case": "requests_2",
                    "request_count": 2,
                    "aggregated": {
                        "mean_tpot_ms": {"mean": case2_tpot, "stddev": 0.2},
                        "output_throughput": {"mean": 150.0},
                    },
                },
            ],
        }

    def test_per_case_rows_and_deltas(self):
        states = [self._state("baseline", 10.0, 20.0), self._state("pr", 11.0, 22.0)]
        rows = bench_compare._compare(states)
        self.assertEqual(len(rows), 4)
        by_key = {(r["label"], r["case"]): r for r in rows}
        base = by_key[("baseline", "default")]
        self.assertEqual(base["request_count"], None)
        self.assertEqual(base["delta_tpot_ms_vs_first"], 0.0)
        pr_default = by_key[("pr", "default")]
        self.assertEqual(pr_default["delta_tpot_ms_vs_first"], 1.0)
        self.assertEqual(pr_default["delta_tpot_pct_vs_first"], 10.0)
        pr_case2 = by_key[("pr", "requests_2")]
        self.assertEqual(pr_case2["request_count"], 2)
        self.assertEqual(pr_case2["delta_tpot_ms_vs_first"], 2.0)
        self.assertEqual(pr_case2["output_throughput"], 150.0)
        # Deltas compare against the first state's *same case*.
        base_case2 = by_key[("baseline", "requests_2")]
        self.assertEqual(base_case2["mean_tpot_ms"], 20.0)


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

    def test_extract_aggregate_compare_end_to_end(self):
        def state(label, tpot_scale):
            raw = dict(self.fixture)
            raw["mean_tpot_ms"] = self.fixture["mean_tpot_ms"] * tpot_scale
            metrics = [_common.extract_metrics(raw) for _ in range(3)]
            return {
                "label": label,
                "cases": [{
                    "case": "default",
                    "request_count": None,
                    "aggregated": bench_compare._aggregate(metrics, 1),
                }],
            }

        states = [state("baseline", 1.0), state("pr", 1.1)]
        agg = states[0]["cases"][0]["aggregated"]
        # Warmup run discarded: 3 runs - 1 warmup = 2 statistical runs.
        self.assertEqual(agg["count"], 2)
        self.assertAlmostEqual(agg["mean_tpot_ms"]["mean"], 28.41, places=4)
        self.assertAlmostEqual(agg["total_token_throughput"]["mean"], 1070.24, places=4)
        self.assertAlmostEqual(agg["spec_decode_acceptance_rate"]["mean"], 0.5702, places=4)
        self.assertNotIn("acceptance_rate", agg)

        rows = bench_compare._compare(states)
        self.assertEqual(len(rows), 2)
        base, pr = rows
        self.assertEqual(base["label"], "baseline")
        self.assertEqual(base["delta_tpot_pct_vs_first"], 0.0)
        self.assertEqual(pr["label"], "pr")
        self.assertAlmostEqual(pr["delta_tpot_pct_vs_first"], 10.0, places=2)
        self.assertEqual(pr["output_throughput"], 535.12)
        self.assertAlmostEqual(pr["spec_decode_acceptance_rate"], 0.5702, places=4)


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
        cfg = _common.BenchConfig(session_id="s", model="/m", health_timeout=1200)
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
        cfg = _common.BenchConfig(session_id="s", model="/m")
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

    def test_bench_compare_rejects_warmup_ge_runs(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as cm:
            bench_compare.main([
                "--model", "/m", "--state", "a=aaa",
                "--runs", "1", "--warmup-runs", "1",
            ])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("must be >= 0 and less than --runs", stderr.getvalue())

    def test_bench_compare_rejects_negative_warmup(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as cm:
            bench_compare.main([
                "--model", "/m", "--state", "a=aaa",
                "--runs", "3", "--warmup-runs", "-1",
            ])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("must be >= 0 and less than --runs", stderr.getvalue())

    def test_allow_stale_native_help_covers_unavailable_digest(self):
        parser = bench_compare.build_parser()
        action = next(
            a for a in parser._actions if "--allow-stale-native" in a.option_strings
        )
        self.assertIn("digest", action.help)
        self.assertIn("unavailable", action.help)


class RemoteHelperTests(unittest.TestCase):
    def test_native_input_digest_parses_remote_output(self):
        proc = subprocess.CompletedProcess(
            [], 0, stdout="digest=abc123\nhead=def456\n", stderr="",
        )
        with mock.patch.object(_common, "ssh_run_script", return_value=proc) as m:
            out = _common.remote_native_input_digest("10.0.0.1", 2222)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["digest"], "abc123")
        self.assertEqual(out["head"], "def456")
        script = m.call_args[0][2]
        self.assertIn("git ls-files", script)
        self.assertIn("csrc", script)

    def test_native_input_digest_covers_untracked_nonignored_files(self):
        # Untracked (but not gitignored) native sources must move the digest;
        # ignored build artifacts stay excluded via --exclude-standard.
        proc = subprocess.CompletedProcess(
            [], 0, stdout="digest=abc123\nhead=def456\n", stderr="",
        )
        with mock.patch.object(_common, "ssh_run_script", return_value=proc) as m:
            _common.remote_native_input_digest("10.0.0.1", 2222)
        script = m.call_args[0][2]
        self.assertIn("--others --exclude-standard", script)
        self.assertIn("sort -z", script)

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

    def test_apply_remote_patch_transfers_base64_and_applies(self):
        with tempfile.TemporaryDirectory() as td:
            patch_file = Path(td) / "p.diff"
            patch_file.write_text("diff --git a/x b/x\n", encoding="utf-8")
            proc = subprocess.CompletedProcess([], 0, stdout=" M vllm_ascend/x.py\n", stderr="")
            with mock.patch.object(_common, "ssh_run_script", return_value=proc) as m:
                out = _common.apply_remote_patch("10.0.0.1", 2222, patch_file)
        self.assertEqual(out["status"], "ok")
        script = m.call_args[0][2]
        self.assertIn("base64", script)
        self.assertIn("git apply", script)
        self.assertIn("git status --short", script)


class BenchCompareMainTests(unittest.TestCase):
    def _run_main(self, extra_args, digests, *, call_order=None):
        cfg = _common.BenchConfig(session_id="s", model="/m")
        written = []
        call_order = call_order if call_order is not None else []

        def fake_write(config, result):
            result["result_path"] = f"/tmp/runs/result_{len(written)}.json"
            written.append(result)
            return Path(result["result_path"])

        digest_iter = iter(digests)

        def fake_digest(*args, **kwargs):
            call_order.append("digest")
            return next(digest_iter)

        def fake_patch(*args, **kwargs):
            call_order.append("patch")
            return {"status": "ok", "changed_files": ["vllm_ascend/csrc/op.cpp"]}

        patches = [
            mock.patch.object(bench_compare, "_get_ssh_endpoint",
                              return_value=("10.0.0.1", 2222)),
            mock.patch.object(bench_compare, "assemble_config", return_value=cfg),
            mock.patch.object(bench_compare, "remote_align_source",
                              return_value={"status": "ok", "heads": {}}),
            mock.patch.object(bench_compare, "remote_native_input_digest",
                              side_effect=fake_digest),
            mock.patch.object(bench_compare, "apply_remote_patch",
                              side_effect=fake_patch),
            mock.patch.object(bench_compare, "call_serve_start",
                              return_value={"status": "ready",
                                            "base_url": "http://127.0.0.1:30001",
                                            "served_model_name": "m"}),
            mock.patch.object(bench_compare, "call_serve_stop",
                              return_value={"status": "stopped"}),
            mock.patch.object(bench_compare, "run_bench_on_remote",
                              return_value={"output_throughput": 100.0,
                                            "mean_tpot_ms": 10.0}),
            mock.patch.object(bench_compare, "write_local_result",
                              side_effect=fake_write),
        ]
        stdout = io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = bench_compare.main([
                    "--model", "/m", "--runs", "1", "--warmup-runs", "0",
                    *extra_args,
                ])
        return rc, json.loads(stdout.getvalue()), written

    def test_native_digest_mismatch_fails_with_partial_results(self):
        rc, out, written = self._run_main(
            ["--state", "baseline=aaa", "--state", "pr=bbb"],
            [
                {"status": "ok", "digest": "d1", "head": "h1"},
                {"status": "ok", "digest": "d2", "head": "h2"},
            ],
        )
        self.assertEqual(rc, 2)
        self.assertEqual(out["status"], "failed")
        self.assertIn("native build inputs", out["error"])
        self.assertIn("--allow-stale-native", out["error"])
        # The completed first state is not lost.
        self.assertEqual(out["partial_states"], ["baseline"])
        self.assertEqual(len(out["result_paths"]), 1)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["label"], "baseline")

    def test_native_digest_mismatch_allowed_with_warning(self):
        rc, out, written = self._run_main(
            ["--state", "baseline=aaa", "--state", "pr=bbb", "--allow-stale-native"],
            [
                {"status": "ok", "digest": "d1", "head": "h1"},
                {"status": "ok", "digest": "d2", "head": "h2"},
            ],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["native_input_changed"])
        self.assertTrue(any("NATIVE INPUTS CHANGED" in w for w in out["warnings"]))
        self.assertEqual(out["states"], ["baseline", "pr"])
        self.assertEqual(len(out["comparison"]), 2)
        self.assertEqual(len(out["result_paths"]), 2)
        # Both states record their native digest for traceability.
        digests = [s["native_input_digest"]["digest"] for s in out["state_results"]]
        self.assertEqual(digests, ["d1", "d2"])

    def test_matching_digests_pass_without_warning(self):
        rc, out, _ = self._run_main(
            ["--state", "baseline=aaa", "--state", "pr=bbb"],
            [
                {"status": "ok", "digest": "d1", "head": "h1"},
                {"status": "ok", "digest": "d1", "head": "h2"},
            ],
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("native_input_changed", out)
        self.assertEqual(out["warnings"], [])

    def test_final_result_includes_config_summary(self):
        rc, out, _ = self._run_main(
            ["--state", "baseline=aaa"],
            [{"status": "ok", "digest": "d1", "head": "h1"}],
        )
        self.assertEqual(rc, 0, out)
        # The effective assembled config is traceable even when preset-driven.
        self.assertEqual(out["config"]["model"], "/m")
        self.assertEqual(out["config"]["session_id"], "s")

    def test_unavailable_native_digest_fails_closed(self):
        rc, out, written = self._run_main(
            ["--state", "baseline=aaa"],
            [{"status": "failed", "digest": None, "error": "ssh timeout"}],
        )
        self.assertEqual(rc, 2)
        self.assertEqual(out["status"], "failed")
        self.assertIn("digest is unavailable", out["error"])
        self.assertIn("--allow-stale-native", out["error"])
        self.assertEqual(written, [])

    def test_unavailable_native_digest_requires_explicit_warning_override(self):
        rc, out, _ = self._run_main(
            ["--state", "baseline=aaa", "--allow-stale-native"],
            [{"status": "failed", "digest": None, "error": "ssh timeout"}],
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out["native_input_unverified"])
        self.assertTrue(any("NATIVE INPUTS UNVERIFIED" in w for w in out["warnings"]))

    def test_remote_patch_is_applied_before_native_digest(self):
        order = []
        rc, out, _ = self._run_main(
            [
                "--state", "baseline=aaa",
                "--remote-patch-file", "/tmp/native-change.patch",
            ],
            [{"status": "ok", "digest": "post-patch", "head": "h1"}],
            call_order=order,
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(order, ["patch", "digest"])
        self.assertEqual(
            out["state_results"][0]["native_input_digest"]["digest"],
            "post-patch",
        )

    def test_skip_parity_flag_removed(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            bench_compare.build_parser().parse_args(
                ["--model", "/m", "--state", "a=b", "--skip-parity"],
            )
        self.assertIn("unrecognized arguments", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
