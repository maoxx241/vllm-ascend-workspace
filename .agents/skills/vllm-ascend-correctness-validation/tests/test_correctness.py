#!/usr/bin/env python3
"""Tests for correctness comparison and normalized harness behavior."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "vllm-ascend-correctness-validation"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


correctness = load_module(
    "_correctness_run_test", SKILL / "scripts" / "correctness_run.py"
)
harness = load_module(
    "_remote_correctness_harness_test",
    SKILL / "scripts" / "remote_correctness_harness.py",
)
aisbench = load_module(
    "_aisbench_adapter_test", SKILL / "scripts" / "aisbench_adapter.py"
)
NOW = "2026-07-25T12:00:00Z"


def case(case_id: str = "case-1", **comparison) -> dict:
    return {
        "id": case_id,
        "mode": "offline-generate",
        "repeats": 1,
        "sampling": {"temperature": 0, "seed": 7},
        "request": {"prompt": "hello"},
        "comparison": comparison,
    }


def result(case_id: str, output: dict, *, metrics: dict | None = None) -> dict:
    return {
        "id": case_id,
        "status": "ok",
        "outputs": [output],
        "metrics": metrics or {},
    }


class ComparisonTests(unittest.TestCase):
    def test_exact_token_match_passes(self) -> None:
        config = {"schema_version": 1, "cases": [case()]}
        baseline = {
            "schema_version": 1,
            "cases": [result("case-1", {"text": "ok", "token_ids": [1, 2]})],
        }
        candidate = json.loads(json.dumps(baseline))
        comparison = correctness.compare_documents(config, baseline, candidate)
        self.assertEqual(comparison["status"], "passed")
        self.assertEqual(comparison["cases"][0]["classification"], "exact_match")

    def test_token_divergence_fails(self) -> None:
        row = correctness.compare_case(
            case(),
            result("case-1", {"token_ids": [1]}),
            result("case-1", {"token_ids": [2]}),
        )
        self.assertEqual(row["classification"], "token_divergence")

    def test_numeric_tolerance_is_distinct_from_exact(self) -> None:
        config = case(atol=0.01, rtol=0.0)
        row = correctness.compare_case(
            config,
            result("case-1", {"token_ids": [1], "numerics": {"logits": [1.0]}}),
            result("case-1", {"token_ids": [1], "numerics": {"logits": [1.001]}}),
        )
        self.assertEqual(
            row["classification"], "numerical_difference_within_tolerance"
        )

    def test_repeat_instability_is_flaky(self) -> None:
        baseline = result("case-1", {"text": "a"})
        baseline["outputs"].append({"text": "b"})
        row = correctness.compare_case(
            case(), baseline, result("case-1", {"text": "a"})
        )
        self.assertEqual(row["classification"], "flaky_or_nondeterministic")

    def test_metric_regression_respects_direction(self) -> None:
        config = case(
            metric_rules={
                "accuracy": {
                    "direction": "higher",
                    "max_absolute_regression": 0.01,
                    "max_relative_regression": 0.02,
                }
            }
        )
        row = correctness.compare_case(
            config,
            result("case-1", {"text": "same"}, metrics={"accuracy": 0.8}),
            result("case-1", {"text": "same"}, metrics={"accuracy": 0.7}),
        )
        self.assertEqual(row["classification"], "task_metric_regression")

    def test_metric_only_aisbench_case_can_pass(self) -> None:
        config = {
            **case(
                metric_rules={
                    "accuracy": {
                        "direction": "higher",
                        "max_absolute_regression": 1.0,
                        "max_relative_regression": 0.02,
                    }
                }
            ),
            "mode": "aisbench",
            "sampling": {},
            "request": {},
        }
        row = correctness.compare_case(
            config,
            result("case-1", {}, metrics={"accuracy": 80.0}),
            result("case-1", {}, metrics={"accuracy": 79.5}),
        )
        self.assertEqual(
            row["classification"], "numerical_difference_within_tolerance"
        )

    def test_full_run_writes_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases_path = root / "source-cases.json"
            cases_path.write_text(
                json.dumps({"schema_version": 1, "cases": [case()]}),
                encoding="utf-8",
            )
            run_dir = root / "run"
            correctness.init_run(
                run_dir,
                run_id="correctness-case-1",
                cases_path=cases_path,
                baseline_label="base",
                candidate_label="candidate",
                created_at=NOW,
            )
            normalized = {
                "schema_version": 1,
                "cases": [result("case-1", {"text": "ok"})],
            }
            baseline = root / "baseline.json"
            candidate = root / "candidate.json"
            baseline.write_text(json.dumps(normalized), encoding="utf-8")
            candidate.write_text(json.dumps(normalized), encoding="utf-8")
            comparison = correctness.compare_run(
                run_dir,
                baseline_path=baseline,
                candidate_path=candidate,
                updated_at=NOW,
            )
            self.assertEqual(comparison["status"], "passed")
            for relative in (
                "manifest.json",
                "cases.json",
                "comparison.json",
                "report.md",
                "reproduction.sh",
                "raw_outputs/baseline.json",
                "raw_outputs/candidate.json",
            ):
                self.assertTrue((run_dir / relative).is_file(), relative)


class HarnessTests(unittest.TestCase):
    def test_workspace_source_roots_precede_outer_repo_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vllm").mkdir()
            (root / "vllm-ascend").mkdir()
            original = list(sys.path)
            original_pythonpath = os.environ.get("PYTHONPATH")
            try:
                sys.path[:] = [str(root), str(root / "vllm"), "sentinel"]
                os.environ["PYTHONPATH"] = f"existing{os.pathsep}{root / 'vllm'}"
                harness._prioritize_workspace_python_packages(root)
                self.assertEqual(
                    sys.path[:3],
                    [str(root / "vllm"), str(root / "vllm-ascend"), str(root)],
                )
                self.assertEqual(sys.path.count(str(root / "vllm")), 1)
                self.assertEqual(
                    os.environ["PYTHONPATH"].split(os.pathsep),
                    [str(root / "vllm"), str(root / "vllm-ascend"), "existing"],
                )
            finally:
                sys.path[:] = original
                if original_pythonpath is None:
                    os.environ.pop("PYTHONPATH", None)
                else:
                    os.environ["PYTHONPATH"] = original_pythonpath

    def test_online_logprobs_are_normalized(self) -> None:
        normalized = harness.normalize_online_response(
            {
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "logprobs": {
                            "content": [
                                {"token": "o", "logprob": -0.1},
                                {"token": "k", "logprob": -0.2},
                            ]
                        },
                    }
                ]
            }
        )
        self.assertEqual(normalized["tokens"], ["o", "k"])
        self.assertEqual(normalized["numerics"]["logprobs"], [-0.1, -0.2])

    def test_unknown_mode_is_normalized_as_unsupported(self) -> None:
        result_document = harness.execute_config(
            {
                "schema_version": 1,
                "label": "test",
                "cases": [{"id": "unsupported-1", "mode": "future-mode"}],
            }
        )
        self.assertEqual(result_document["cases"][0]["status"], "unsupported")


class AisbenchAdapterTests(unittest.TestCase):
    def test_prepare_does_not_modify_benchmark_tree_or_embed_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = aisbench.prepare(
                root / "adapter",
                host="127.0.0.1",
                port=8000,
                served_model="example",
                datasets=["demo_gsm8k_gen_4_shot_cot_chat_prompt"],
                work_dir=Path("/remote/output"),
                metric="accuracy",
                direction="higher",
                max_absolute_regression=0.5,
                max_relative_regression=0.01,
                max_out_len=64,
                batch_size=1,
                temperature=0.0,
                seed=7,
                num_prompts=8,
            )
            model_config = Path(payload["model_config"])
            self.assertTrue(model_config.is_file())
            text = model_config.read_text(encoding="utf-8")
            self.assertIn('api_key=""', text)
            self.assertNotIn("benchmark/ais_bench/benchmark/configs", str(model_config))
            ast.parse(text)
            self.assertEqual(payload["command"][0], "ais_bench")
            self.assertIn("--config-dir", payload["command"])
            cases = json.loads(
                (root / "adapter" / "aisbench-cases.json").read_text(encoding="utf-8")
            )
            correctness.validate_cases_document(cases)

    def test_summary_csv_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.csv"
            summary.write_text(
                "dataset,version,metric,mode,vaws-correctness\n"
                "gsm8k,abc123,accuracy,gen,56.7\n",
                encoding="utf-8",
            )
            normalized = aisbench.normalize_summary(summary, label="baseline")
            self.assertEqual(normalized["cases"][0]["status"], "ok")
            self.assertEqual(normalized["cases"][0]["metrics"]["accuracy"], 56.7)


if __name__ == "__main__":
    unittest.main()
