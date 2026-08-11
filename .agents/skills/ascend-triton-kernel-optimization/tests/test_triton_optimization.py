#!/usr/bin/env python3
"""Tests for Ascend Triton optimization round control."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "ascend-triton-kernel-optimization"
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import add_artifact, new_manifest, sha256_file, transition_status, write_manifest


def load_module():
    name = "_triton_optimization_test"
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / "triton_optimization.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


optimization = load_module()
NOW = "2026-08-03T12:00:00Z"


def passed_manifest(path: Path, run_id: str, parent: str, kernel: Path, case_ids: list[str]) -> None:
    matrix = {
        "schema_version": 1,
        "kernel": {"path": str(kernel), "sha256": sha256_file(kernel)},
        "cases": [{"id": case_id} for case_id in case_ids],
    }
    matrix_path = path.parent / f"{path.stem}-case-matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    manifest = new_manifest(run_type="correctness", run_id=run_id, parent_run_id=parent, created_at=NOW)
    manifest = add_artifact(
        manifest,
        name="kernel",
        kind="triton-kernel",
        uri=str(kernel),
        sha256=sha256_file(kernel),
        updated_at=NOW,
    )
    manifest = add_artifact(
        manifest,
        name="case-matrix",
        kind="case-matrix",
        uri=str(matrix_path),
        updated_at=NOW,
    )
    manifest = transition_status(manifest, "running", updated_at=NOW)
    manifest = transition_status(manifest, "passed", updated_at=NOW)
    write_manifest(path, manifest)


class OptimizationTests(unittest.TestCase):
    def test_keep_updates_best_and_meets_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "kernel.py"
            kernel.write_text("baseline", encoding="utf-8")
            validation_path = root / "start-validation.json"
            passed_manifest(validation_path, "start-validation", "triton-softmax-001", kernel, ["case-1"])
            config = {
                "schema_version": 1,
                "run_id": "softmax-optimization-001",
                "parent_run_id": "triton-softmax-001",
                "op_name": "softmax",
                "kernel": {"path": str(kernel)},
                "validation_manifest": str(validation_path),
                "target": {"soc": "Ascend910B2"},
                "cases": [{"id": "case-1", "weight": 1.0}],
                "baseline": [{"case_id": "case-1", "median_us": 10.0, "repeats": 50}],
                "objective": {"target_relative_improvement": 0.1, "min_relative_improvement": 0.01, "noise_floor": 0.005, "max_case_regression": 0.02},
                "max_rounds": 3,
                "max_consecutive_failures": 2,
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "optimization"
            planned = optimization.plan(output, config_path=config_path, created_at=NOW)
            candidate = root / "candidate.py"
            candidate.write_text("candidate", encoding="utf-8")
            round_validation = root / "round-validation.json"
            passed_manifest(round_validation, "round-validation", "softmax-optimization-001", candidate, ["case-1"])
            round_result = {
                "schema_version": 1,
                "round": 1,
                "parent_kernel_sha256": planned["kernel_sha256"],
                "candidate": {"path": str(candidate)},
                "hypothesis": "reduce scalar address work",
                "change": "linearize indices",
                "verification": {"manifest": str(round_validation)},
                "measurements": [{"case_id": "case-1", "median_us": 8.0, "repeats": 50}],
            }
            round_path = root / "round.json"
            round_path.write_text(json.dumps(round_result), encoding="utf-8")
            recorded = optimization.record(output, result_path=round_path, recorded_at=NOW)
            self.assertEqual(recorded["decision"], "KEEP")
            self.assertTrue(recorded["target_met"])
            analyzed = optimization.analyze(output, updated_at=NOW)
            self.assertEqual(analyzed["status"], "passed")

    def test_parent_hash_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = root / "kernel.py"
            kernel.write_text("baseline", encoding="utf-8")
            validation_path = root / "validation.json"
            passed_manifest(validation_path, "start-validation", "parent", kernel, ["case"])
            config = {
                "schema_version": 1, "run_id": "optimization", "parent_run_id": "parent", "op_name": "op",
                "kernel": {"path": str(kernel)}, "validation_manifest": str(validation_path), "target": {"soc": "Ascend910B2"},
                "cases": [{"id": "case", "weight": 1}], "baseline": [{"case_id": "case", "median_us": 10, "repeats": 5}],
                "objective": {"target_relative_improvement": 0.1, "min_relative_improvement": 0.01, "noise_floor": 0.005, "max_case_regression": 0.02},
                "max_rounds": 2, "max_consecutive_failures": 2,
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "optimization"
            optimization.plan(output, config_path=config_path, created_at=NOW)
            candidate = root / "candidate.py"
            candidate.write_text("candidate", encoding="utf-8")
            round_validation = root / "round-validation.json"
            passed_manifest(round_validation, "round-validation", "optimization", candidate, ["case"])
            result = {"schema_version": 1, "round": 1, "parent_kernel_sha256": "0" * 64, "candidate": {"path": str(candidate)}, "hypothesis": "x", "change": "y", "verification": {"manifest": str(round_validation)}}
            result_path = root / "round.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            with self.assertRaisesRegex(optimization.OptimizationError, "current best"):
                optimization.record(output, result_path=result_path, recorded_at=NOW)


if __name__ == "__main__":
    unittest.main()
