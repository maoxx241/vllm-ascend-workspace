#!/usr/bin/env python3
"""Tests for Ascend Triton workflow orchestration."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "ascend-triton-workflow"
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import new_manifest, transition_status, write_manifest


def load_module():
    name = "_triton_workflow_test"
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / "triton_workflow.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


workflow = load_module()
NOW = "2026-08-03T12:00:00Z"


def config() -> dict:
    return {
        "schema_version": 1,
        "run_id": "triton-softmax-001",
        "op_name": "softmax",
        "source": {"kind": "gpu-triton", "path": "/src/softmax.py"},
        "target": {"soc": "Ascend910B2"},
        "required_stages": ["development", "validation"],
    }


class WorkflowTests(unittest.TestCase):
    def test_optimization_requires_validation(self) -> None:
        payload = config()
        payload["required_stages"] = ["optimization"]
        with self.assertRaisesRegex(workflow.WorkflowError, "requires validation"):
            workflow.validate_config(payload)

    def test_full_lifecycle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "workflow"
            workflow.plan(output, config_path=config_path, created_at=NOW)
            for stage, run_type in (("development", "debug"), ("validation", "correctness")):
                child = new_manifest(
                    run_type=run_type,
                    run_id=f"{stage}-child",
                    parent_run_id="triton-softmax-001",
                    created_at=NOW,
                )
                child = transition_status(child, "running", updated_at=NOW)
                child = transition_status(child, "passed", updated_at=NOW)
                child_path = root / f"{stage}.json"
                write_manifest(child_path, child)
                workflow.link(output, stage=stage, child_path=child_path, updated_at=NOW)
            result = workflow.finalize(output, updated_at=NOW)
            self.assertEqual(result["status"], "passed")

    def test_missing_stage_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "workflow"
            workflow.plan(output, config_path=config_path, created_at=NOW)
            result = workflow.finalize(output, updated_at=NOW)
            self.assertEqual(result["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
