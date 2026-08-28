#!/usr/bin/env python3
"""Tests for the isolated Ascend operator case controller."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "ascend-operator-debug"


def load_module():
    name = "_operator_debug_test"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / "operator_debug.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


operator = load_module()
NOW = "2026-07-25T12:00:00Z"


def case(case_id: str, mode: str, dtype: str = "float16") -> dict:
    return {
        "id": case_id,
        "mode": mode,
        "inputs": [
            {
                "name": "x",
                "shape": [2, 4],
                "strides": [4, 1],
                "dtype": dtype,
                "layout": "ND",
            }
        ],
        "attributes": {"transpose": False},
    }


def config() -> dict:
    return {
        "schema_version": 1,
        "run_id": "operator-case-1",
        "operator": {
            "name": "npu_example",
            "invocation": "torch_npu.npu_example(x)",
            "reference": "torch_ref(x.cpu())",
        },
        "tolerance": {"atol": 0.001, "rtol": 0.001},
        "source_model_failure": "model output diverges",
        "environment": {"torch_npu": "test"},
        "cases": [
            case("fp16-eager", "eager"),
            case("fp16-graph", "graph"),
        ],
    }


def result(case_id: str, status: str = "passed") -> dict:
    payload = {
        "schema_version": 1,
        "case_id": case_id,
        "status": status,
        "comparisons": [
            {
                "output": "out",
                "max_abs": 0.0,
                "max_rel": 0.0,
                "cosine": 1.0,
            }
        ],
    }
    if status in {"crash", "unsupported"}:
        payload["error"] = "operator failed"
        payload["comparisons"] = []
    return payload


class OperatorDebugTests(unittest.TestCase):
    def test_duplicate_case_ids_are_rejected(self) -> None:
        invalid = config()
        invalid["cases"][1]["id"] = invalid["cases"][0]["id"]
        with self.assertRaisesRegex(operator.OperatorDebugError, "duplicated"):
            operator.validate_config(invalid)

    def test_failure_axes_localize_graph_only_mismatch(self) -> None:
        cfg = config()
        matrix = {"cases": [{**row, "status": "recorded"} for row in cfg["cases"]]}
        results = {
            "results": [
                result("fp16-eager"),
                result("fp16-graph", "numerical_mismatch"),
            ]
        }
        analysis = operator.analyze_documents(cfg, matrix, results)
        self.assertEqual(analysis["status"], "diagnosed")
        self.assertEqual(
            analysis["failure_axes"]["mode"]["graph"], ["fp16-graph"]
        )

    def test_missing_case_is_inconclusive(self) -> None:
        cfg = config()
        matrix = {"cases": [{**row, "status": "pending"} for row in cfg["cases"]]}
        analysis = operator.analyze_documents(
            cfg, matrix, {"results": [result("fp16-eager")]}
        )
        self.assertEqual(analysis["status"], "inconclusive")
        self.assertEqual(analysis["missing_cases"], ["fp16-graph"])

    def test_all_isolated_cases_pass_points_to_integration(self) -> None:
        cfg = config()
        matrix = {"cases": [{**row, "status": "recorded"} for row in cfg["cases"]]}
        analysis = operator.analyze_documents(
            cfg,
            matrix,
            {"results": [result("fp16-eager"), result("fp16-graph")]},
        )
        self.assertEqual(analysis["status"], "passed")
        self.assertIsNotNone(analysis["integration_hint"])

    def test_full_lifecycle_rejects_duplicate_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "case"
            operator.plan(output, config_path=config_path, created_at=NOW)
            for case_id in ("fp16-eager", "fp16-graph"):
                result_path = root / f"{case_id}.json"
                result_path.write_text(
                    json.dumps(result(case_id)), encoding="utf-8"
                )
                operator.record(output, result_path=result_path, recorded_at=NOW)
                with self.assertRaisesRegex(operator.OperatorDebugError, "already"):
                    operator.record(
                        output, result_path=result_path, recorded_at=NOW
                    )
            analyzed = operator.analyze(output, updated_at=NOW)
            self.assertEqual(analyzed["status"], "passed")
            self.assertTrue((output / "report.md").is_file())


if __name__ == "__main__":
    unittest.main()
