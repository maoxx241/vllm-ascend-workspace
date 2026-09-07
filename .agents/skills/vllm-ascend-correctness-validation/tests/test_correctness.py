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


def execution(**engine_args) -> dict:
    return {
        "model": "/models/example",
        "engine_args": {"tensor_parallel_size": 2, **engine_args},
        "base_url": None,
        "served_model": None,
        "cases_sha256": "a" * 64,
    }


def recorded_observation(**overrides) -> dict:
    payload = {
        "workspace_snapshot": {"vllm_ascend_commit": "aaaa1111"},
        "environment": {"cann": "test", "torch_npu": "test"},
        "model": {"path": "/models/example", "weight_hash": "ab" * 32},
        "topology": {"tp": 2, "dp": 1},
        "native_digest": "cd" * 32,
    }
    payload.update(overrides)
    return payload


def result_document(
    label: str,
    cases: list[dict],
    *,
    execution_block: dict | None = None,
    observation: dict | bool | None = True,
) -> dict:
    document = {"schema_version": 1, "label": label, "cases": cases}
    if execution_block is not None:
        document["execution"] = execution_block
    if observation is True:
        document["observation"] = recorded_observation()
    elif isinstance(observation, dict):
        document["observation"] = observation
    return document


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def init_run(root: Path, **overrides) -> Path:
    cases_path = write_json(
        root / "source-cases.json", {"schema_version": 1, "cases": [case()]}
    )
    run_dir = root / "run"
    arguments = {
        "run_id": "correctness-case-1",
        "cases_path": cases_path,
        "baseline_label": "base",
        "candidate_label": "candidate",
        "created_at": NOW,
    }
    arguments.update(overrides)
    correctness.init_run(run_dir, **arguments)
    return run_dir


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

    def test_empty_text_is_not_agreement(self) -> None:
        row = correctness.compare_case(
            case(),
            result("case-1", {"text": ""}),
            result("case-1", {"text": ""}),
        )
        self.assertEqual(row["classification"], "infrastructure_failure")
        self.assertEqual(row["details"]["reason"], "empty-output-is-not-agreement")

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
            run_dir = init_run(root, parent_run_id="change-validation-1")
            cases = [result("case-1", {"text": "ok"})]
            baseline = write_json(
                root / "baseline.json",
                result_document("base", cases, execution_block=execution()),
            )
            candidate = write_json(
                root / "candidate.json",
                result_document("candidate", cases, execution_block=execution()),
            )
            comparison = correctness.compare_run(
                run_dir,
                baseline_path=baseline,
                candidate_path=candidate,
                updated_at=NOW,
            )
            self.assertEqual(comparison["status"], "passed")
            self.assertTrue(comparison["execution"]["identical_execution"])
            for relative in (
                "manifest.json",
                "cases.json",
                "comparison.json",
                "execution.json",
                "report.md",
                "reproduction.sh",
                "raw_outputs/baseline.json",
                "raw_outputs/candidate.json",
            ):
                self.assertTrue((run_dir / relative).is_file(), relative)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["parent_run_id"], "change-validation-1")
            artifacts = {row["name"]: row for row in manifest["artifacts"]}
            self.assertIn("execution-identity", artifacts)
            self.assertIn("comparability-certificate", artifacts)
            certificate = json.loads(
                (run_dir / "comparability-certificate.json").read_text(encoding="utf-8")
            )
            self.assertEqual(certificate["verdict"], "comparable")
            for name in (
                "baseline-output",
                "candidate-output",
                "execution-identity",
                "comparability-certificate",
            ):
                self.assertRegex(artifacts[name]["sha256"], r"^[0-9a-f]{64}$")

    def test_empty_text_cannot_pass_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root)
            cases = [result("case-1", {"text": ""})]
            baseline = write_json(
                root / "baseline.json",
                result_document("base", cases, execution_block=execution()),
            )
            candidate = write_json(
                root / "candidate.json",
                result_document("candidate", cases, execution_block=execution()),
            )
            comparison = correctness.compare_run(
                run_dir,
                baseline_path=baseline,
                candidate_path=candidate,
                updated_at=NOW,
            )
            self.assertEqual(comparison["status"], "inconclusive")
            self.assertEqual(
                comparison["cases"][0]["details"]["reason"],
                "empty-output-is-not-agreement",
            )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotEqual(manifest["status"], "passed")

    def test_missing_observation_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root)
            cases = [result("case-1", {"text": "ok"})]
            baseline = write_json(
                root / "baseline.json",
                result_document(
                    "base", cases, execution_block=execution(), observation=False
                ),
            )
            candidate = write_json(
                root / "candidate.json",
                result_document(
                    "candidate",
                    cases,
                    execution_block=execution(),
                    observation=False,
                ),
            )
            with self.assertRaisesRegex(
                correctness.CorrectnessError, "not-comparable"
            ):
                correctness.compare_run(
                    run_dir,
                    baseline_path=baseline,
                    candidate_path=candidate,
                    updated_at=NOW,
                )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "planned")
            self.assertFalse((run_dir / "comparison.json").exists())

    def test_declaration_mismatch_cannot_pass_after_consume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root, topology={"tp": 8, "dp": 1})
            cases = [result("case-1", {"text": "ok"})]
            baseline = write_json(
                root / "baseline.json",
                result_document("base", cases, execution_block=execution()),
            )
            candidate = write_json(
                root / "candidate.json",
                result_document("candidate", cases, execution_block=execution()),
            )
            with self.assertRaisesRegex(
                correctness.CorrectnessError, "declaration/observation mismatch"
            ):
                correctness.compare_run(
                    run_dir,
                    baseline_path=baseline,
                    candidate_path=candidate,
                    updated_at=NOW,
                )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotEqual(manifest["status"], "passed")
            self.assertFalse((run_dir / "comparison.json").exists())
            certificate = json.loads(
                (run_dir / "comparability-certificate.json").read_text(encoding="utf-8")
            )
            self.assertEqual(certificate["verdict"], "not-comparable")
            self.assertTrue(certificate["declaration_mismatches"])
            for side in ("baseline", "candidate"):
                self.assertTrue(certificate[side].get("declaration_mismatches"))

    def test_null_observed_identity_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root)
            cases = [result("case-1", {"text": "ok"})]
            observation = {
                "workspace_snapshot": {"vllm_ascend_commit": None},
                "environment": {"cann": None},
                "model": {"weight_hash": None},
                "topology": {"tp": None},
                "native_digest": None,
            }
            baseline = write_json(
                root / "baseline.json",
                result_document(
                    "base",
                    cases,
                    execution_block=execution(),
                    observation=observation,
                ),
            )
            candidate = write_json(
                root / "candidate.json",
                result_document(
                    "candidate",
                    cases,
                    execution_block=execution(),
                    observation=observation,
                ),
            )
            with self.assertRaisesRegex(
                correctness.CorrectnessError, "not-comparable"
            ):
                correctness.compare_run(
                    run_dir,
                    baseline_path=baseline,
                    candidate_path=candidate,
                    updated_at=NOW,
                )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotEqual(manifest["status"], "passed")
            self.assertFalse((run_dir / "comparison.json").exists())
            certificate = json.loads(
                (run_dir / "comparability-certificate.json").read_text(encoding="utf-8")
            )
            self.assertEqual(certificate["verdict"], "not-comparable")
            unknown_fields = {
                (item["side"], item["key"]) for item in certificate["unknowns"]
            }
            self.assertIn(("baseline", "workspace_snapshot"), unknown_fields)
            self.assertIn(("candidate", "environment"), unknown_fields)
            self.assertIn(("baseline", "native_digest"), unknown_fields)


class ExecutionIdentityTests(unittest.TestCase):
    def test_undeclared_engine_args_difference_is_refused(self) -> None:
        """Regression: eager-vs-graph used to be reported as a code regression."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root)
            baseline = write_json(
                root / "baseline.json",
                result_document(
                    "base",
                    [result("case-1", {"token_ids": [1, 2]})],
                    execution_block=execution(enforce_eager=True),
                ),
            )
            candidate = write_json(
                root / "candidate.json",
                result_document(
                    "candidate",
                    [result("case-1", {"token_ids": [1, 3]})],
                    execution_block=execution(enforce_eager=False),
                ),
            )
            with self.assertRaisesRegex(
                correctness.CorrectnessError,
                r"undeclared fields: engine_args\.enforce_eager.*--allowed-difference",
            ):
                correctness.compare_run(
                    run_dir, baseline_path=baseline, candidate_path=candidate, updated_at=NOW
                )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "planned")
            self.assertFalse((run_dir / "comparison.json").exists())

    def test_declared_difference_is_recorded_with_the_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root, allowed_differences=["engine_args.enforce_eager"])
            baseline = write_json(
                root / "baseline.json",
                result_document(
                    "base",
                    [result("case-1", {"token_ids": [1, 2]})],
                    execution_block=execution(enforce_eager=True),
                ),
            )
            candidate = write_json(
                root / "candidate.json",
                result_document(
                    "candidate",
                    [result("case-1", {"token_ids": [1, 3]})],
                    execution_block=execution(enforce_eager=False),
                ),
            )
            comparison = correctness.compare_run(
                run_dir, baseline_path=baseline, candidate_path=candidate, updated_at=NOW
            )
            self.assertEqual(comparison["status"], "failed")
            self.assertEqual(comparison["primary_classification"], "token_divergence")
            identity = comparison["execution"]
            self.assertEqual(identity["allowed_differences"], ["engine_args.enforce_eager"])
            self.assertEqual(
                [row["key"] for row in identity["observed_differences"]],
                ["engine_args.enforce_eager"],
            )
            recorded = json.loads((run_dir / "execution.json").read_text(encoding="utf-8"))
            self.assertEqual(recorded["baseline"]["execution"]["engine_args"]["enforce_eager"], True)
            self.assertEqual(recorded["candidate"]["execution"]["engine_args"]["enforce_eager"], False)
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("## Execution identity", report)
            self.assertIn("engine_args.enforce_eager", report)

    def test_result_without_execution_block_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root)
            cases = [result("case-1", {"text": "ok"})]
            baseline = write_json(root / "baseline.json", result_document("base", cases))
            candidate = write_json(
                root / "candidate.json",
                result_document("candidate", cases, execution_block=execution()),
            )
            with self.assertRaisesRegex(
                correctness.CorrectnessError, "baseline result has no execution block"
            ):
                correctness.compare_run(
                    run_dir, baseline_path=baseline, candidate_path=candidate, updated_at=NOW
                )

    def test_same_file_passed_twice_is_refused_by_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root)
            same = write_json(
                root / "same.json",
                result_document(
                    "base", [result("case-1", {"text": "ok"})], execution_block=execution()
                ),
            )
            with self.assertRaisesRegex(
                correctness.CorrectnessError, "candidate result label is 'base'"
            ):
                correctness.compare_run(
                    run_dir, baseline_path=same, candidate_path=same, updated_at=NOW
                )

    def test_different_case_arrays_are_an_undeclared_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = init_run(root)
            cases = [result("case-1", {"text": "ok"})]
            left = execution()
            right = execution()
            right["cases_sha256"] = "b" * 64
            baseline = write_json(
                root / "baseline.json", result_document("base", cases, execution_block=left)
            )
            candidate = write_json(
                root / "candidate.json",
                result_document("candidate", cases, execution_block=right),
            )
            with self.assertRaisesRegex(
                correctness.CorrectnessError, "undeclared fields: cases_sha256"
            ):
                correctness.compare_run(
                    run_dir, baseline_path=baseline, candidate_path=candidate, updated_at=NOW
                )

    def test_identical_labels_are_rejected_at_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(correctness.CorrectnessError, "must differ"):
                init_run(Path(tmp), baseline_label="same", candidate_label="same")


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
        document = harness.execute_config(
            {
                "schema_version": 1,
                "label": "test",
                "cases": [{"id": "unsupported-1", "mode": "future-mode"}],
            }
        )
        self.assertEqual(document["cases"][0]["status"], "unsupported")

    def test_result_records_engine_args_and_case_digest(self) -> None:
        """Regression: engine_args used to reach neither result nor manifest."""
        cases = [{"id": "unsupported-1", "mode": "future-mode"}]
        config = {
            "schema_version": 1,
            "label": "candidate",
            "model": "/models/example",
            "engine_args": {"enforce_eager": False, "tensor_parallel_size": 2},
            "base_url": "http://service.invalid:8000",
            "served_model": "example",
            "cases": cases,
        }
        document = harness.execute_config(config)
        self.assertEqual(
            document["execution"]["engine_args"],
            {"enforce_eager": False, "tensor_parallel_size": 2},
        )
        self.assertEqual(document["execution"]["model"], "/models/example")
        self.assertEqual(document["execution"]["served_model"], "example")
        self.assertEqual(document["execution"]["cases_sha256"], harness.cases_sha256(cases))
        correctness.validate_execution_block(document, label="candidate")

    def test_non_object_engine_args_are_rejected_before_execution(self) -> None:
        with self.assertRaisesRegex(harness.HarnessError, "engine_args"):
            harness.execute_config(
                {
                    "schema_version": 1,
                    "label": "x",
                    "engine_args": ["--enforce-eager"],
                    "cases": [{"id": "c", "mode": "future-mode"}],
                }
            )


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
            normalized = aisbench.normalize_summary(
                summary,
                label="baseline",
                execution={"served_model": "example", "engine_args": {"enforce_eager": True}},
            )
            self.assertEqual(normalized["cases"][0]["status"], "ok")
            self.assertEqual(normalized["cases"][0]["metrics"]["accuracy"], 56.7)
            self.assertEqual(normalized["execution"]["engine_args"], {"enforce_eager": True})
            correctness.validate_execution_block(normalized, label="baseline")

    def test_normalize_requires_execution_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.csv"
            summary.write_text(
                "dataset,version,metric,mode,vaws-correctness\n"
                "gsm8k,abc123,accuracy,gen,56.7\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                aisbench.AisbenchAdapterError, "execution.engine_args must be an object"
            ):
                aisbench.normalize_summary(
                    summary, label="baseline", execution={"served_model": "example"}
                )
            with self.assertRaisesRegex(
                aisbench.AisbenchAdapterError, "execution.served_model"
            ):
                aisbench.normalize_summary(
                    summary, label="baseline", execution={"engine_args": {}}
                )


if __name__ == "__main__":
    unittest.main()
