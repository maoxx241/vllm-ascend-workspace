#!/usr/bin/env python3
"""Tests for Ascend Triton development control-plane logic."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "ascend-triton-operator-development"
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import add_artifact, new_manifest, sha256_file, transition_status, write_manifest


def load_module():
    name = "_triton_development_test"
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / "triton_development.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


development = load_module()
NOW = "2026-08-03T12:00:00Z"


def config() -> dict:
    return {
        "schema_version": 1,
        "run_id": "softmax-development-001",
        "parent_run_id": "triton-softmax-001",
        "op_name": "softmax",
        "mode": "gpu-migration",
        "source": {"kind": "gpu-triton", "path": "/src/softmax.py"},
        "reference": {"path": "/src/ref.py"},
        "target": {"soc": "Ascend910B2"},
        "tolerances": {"float16": {"atol": 0.001, "rtol": 0.001}},
        "cases": [
            {
                "id": "case-1",
                "inputs": [
                    {"name": "x", "shape": [2, 4], "strides": [4, 1], "dtype": "float16", "layout": "ND"}
                ],
            }
        ],
    }


class DevelopmentTests(unittest.TestCase):
    def test_duplicate_cases_rejected(self) -> None:
        payload = config()
        payload["cases"].append(payload["cases"][0])
        with self.assertRaisesRegex(development.DevelopmentError, "duplicated"):
            development.validate_config(payload)

    def test_finalize_follows_validation_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "development"
            development.plan(output, config_path=config_path, created_at=NOW)
            kernel = root / "kernel.py"
            kernel.write_text("import triton\n", encoding="utf-8")
            validation = new_manifest(
                run_type="correctness",
                run_id="validation-child",
                parent_run_id="triton-softmax-001",
                created_at=NOW,
            )
            matrix_path = root / "case-matrix.json"
            matrix_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kernel": {"path": str(kernel), "sha256": sha256_file(kernel)},
                        "cases": [{"id": "case-1"}],
                    }
                ),
                encoding="utf-8",
            )
            validation = add_artifact(
                validation,
                name="kernel",
                kind="triton-kernel",
                uri=str(kernel),
                sha256=sha256_file(kernel),
                updated_at=NOW,
            )
            validation = add_artifact(
                validation,
                name="case-matrix",
                kind="case-matrix",
                uri=str(matrix_path),
                updated_at=NOW,
            )
            validation = transition_status(validation, "running", updated_at=NOW)
            validation = transition_status(validation, "passed", updated_at=NOW)
            validation_path = root / "validation.json"
            write_manifest(validation_path, validation)
            result = development.finalize(
                output,
                kernel=kernel,
                semantic_report=output / "semantic-report.md",
                sketch=output / "sketch.md",
                validation_manifest=validation_path,
                updated_at=NOW,
            )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(result["kernel_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
