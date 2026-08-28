#!/usr/bin/env python3
"""Tests for Ascend Triton static and correctness gates."""

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


static = load_script("validate_triton_impl", "validate_triton_impl.py")
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
    def test_static_gate_detects_fallback(self) -> None:
        tree = __import__("ast").parse(kernel_source(fallback=True))
        result = static.analyze_tree(tree)
        self.assertFalse(result["valid"])
        self.assertFalse(result["checks"]["no_pytorch_fallback"]["passed"])

    def test_full_lifecycle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "kernel.py"
            kernel.write_text(kernel_source(), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "validation"
            validation.plan(output, config_path=config_path, kernel=kernel, created_at=NOW)
            result_path = root / "result.json"
            result_path.write_text(json.dumps({"schema_version": 1, "case_id": "case-1", "status": "passed", "comparisons": [{"output": "out", "max_abs": 0.0, "max_rel": 0.0, "cosine": 1.0}]}), encoding="utf-8")
            validation.record(output, result_path=result_path, recorded_at=NOW)
            result = validation.analyze(output, updated_at=NOW)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["passed_cases"], 1)

    def test_missing_case_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "kernel.py"
            kernel.write_text(kernel_source(), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "validation"
            validation.plan(output, config_path=config_path, kernel=kernel, created_at=NOW)
            result = validation.analyze(output, updated_at=NOW)
            self.assertEqual(result["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
