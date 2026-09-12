#!/usr/bin/env python3
"""Tests for Ascend Triton source lint and runtime evidence reports."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "ascend-triton-kernel-validation"


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


static = load_script("lint_triton_source", "lint_triton_source.py")
validation = load_script("_triton_validation_test", "triton_validation.py")
NOW = "2026-08-03T12:00:00Z"


def kernel_source(fallback: bool = False) -> str:
    body = "return torch.sum(x)" if fallback else "out = torch.empty_like(x)\n        _kernel[(1,)](x, out, 1)\n        return out"
    return (
        "import torch\nimport triton\nimport triton.language as tl\n"
        "@triton.jit\ndef _kernel(x, out, n: tl.constexpr):\n    tl.store(out, tl.load(x))\n"
        "class ModelNew:\n    def forward(self, x):\n        " + body + "\n"
    )


def config() -> dict:
    return {
        "schema_version": 1,
        "run_id": "softmax-validation-001",
        "parent_run_id": "triton-softmax-001",
        "op_name": "softmax",
        "reference": {"path": "/src/ref.py"},
        "target": {"soc": "Ascend910B2"},
        "tolerances": {"float16": {"atol": 0.001, "rtol": 0.001}},
        "cases": [
            {
                "id": "case-1",
                "mode": "eager",
                "inputs": [{"name": "x", "shape": [2, 4], "strides": [4, 1], "dtype": "float16", "layout": "ND"}],
            }
        ],
    }


class ValidationTests(unittest.TestCase):
    def test_one_call_with_business_config_retains_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = config()
            for key in ("schema_version", "run_id", "parent_run_id"):
                cfg.pop(key, None)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            kernel = root / "kernel.py"
            kernel.write_text(kernel_source(), encoding="utf-8")
            result = validation.build_report(config_path, [], kernel=kernel, output_dir=root / "report")
            self.assertEqual(result["status"], "inconclusive")
            manifest = json.loads(Path(result["manifest_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "inconclusive")
            self.assertEqual(list((root / ".vaws-local/report-inputs").glob("*.json")), [])

    def test_function_operator_report_is_not_blocked_by_wrapper_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            kernel = root / "kernel.py"
            kernel.write_text("from launchers import run_kernel\ndef operator(x):\n    return run_kernel(x)\n", encoding="utf-8")
            result = validation.build_report(config_path, [], kernel=kernel, output_dir=root / "report")
            self.assertEqual(result["status"], "inconclusive")
            analysis = json.loads(Path(result["analysis"]).read_text(encoding="utf-8"))
            self.assertTrue(analysis["source_lint"]["advisory"])
            self.assertEqual(analysis["source_lint"]["status"], "out_of_scope")

    def test_source_lint_detects_fallback(self) -> None:
        tree = __import__("ast").parse(kernel_source(fallback=True))
        result = static.lint_tree(tree)
        self.assertEqual(result["pytorch_fallback"], "unknown")
        self.assertTrue(any(item["call"].startswith("torch.") and item["line"] > 0 for item in result["potential_compute_calls"]))

    def test_full_lifecycle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "kernel.py"
            kernel.write_text(kernel_source(), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "validation"
            validation._prepare_report(output, config_path=config_path, kernel=kernel, created_at=NOW)
            result_path = root / "result.json"
            result_path.write_text(json.dumps({"schema_version": 1, "case_id": "case-1", "status": "passed", "comparisons": [{"output": "out", "max_abs": 0.0, "max_rel": 0.0, "cosine": 1.0}]}), encoding="utf-8")
            validation._record_result(output, result_path=result_path, recorded_at=NOW)
            result = validation._analyze_report(output, updated_at=NOW)
            self.assertEqual(result["status"], "inconclusive")
            self.assertEqual(result["numerical_status"], "passed")
            self.assertEqual(result["candidate_execution"], "unknown")
            self.assertEqual(result["passed_cases"], 1)

    def test_reference_fallback_results_do_not_prove_candidate_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            kernel = root / "kernel.py"
            kernel.write_text(kernel_source(fallback=True), encoding="utf-8")
            result_path = root / "reference-result.json"
            result_path.write_text(json.dumps({"schema_version": 1, "case_id": "case-1", "status": "passed", "comparisons": [{"output": "out", "max_abs": 0.0}]}), encoding="utf-8")
            result = validation.build_report(config_path, [result_path], kernel=kernel, output_dir=root / "report")
            self.assertEqual(result["numerical_status"], "passed")
            self.assertEqual(result["candidate_execution"], "unknown")
            self.assertEqual(result["status"], "inconclusive")
            manifest = json.loads(Path(result["manifest_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "inconclusive")

    def test_missing_case_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "kernel.py"
            kernel.write_text(kernel_source(), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "validation"
            validation._prepare_report(output, config_path=config_path, kernel=kernel, created_at=NOW)
            result = validation._analyze_report(output, updated_at=NOW)
            self.assertEqual(result["status"], "inconclusive")



# This suite exercises report semantics, not coordinator Git snapshotting.
# Real code-identity tests belong to the coordinator package.
from unittest.mock import patch as _patch_report_code
_REPORT_CODE = {"source_head": "1" * 40, "snapshot_commit": "2" * 40, "dirty": True}
_report_code_patch = _patch_report_code("vaws_coordinator.code_identity.manifest_code", return_value=_REPORT_CODE)


def setup_module():
    _report_code_patch.start()


def teardown_module():
    _report_code_patch.stop()


setUpModule = setup_module
tearDownModule = teardown_module

if __name__ == "__main__":
    unittest.main()
