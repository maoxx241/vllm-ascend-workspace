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

from vaws_coordinator.run_manifest import add_artifact, new_manifest, transition_status, write_manifest


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
            workflow._prepare_report(output, config_path=config_path, created_at=NOW)
            for stage, run_type in (("development", "debug"), ("validation", "correctness")):
                child = new_manifest(
                    run_type=run_type,
                    run_id=f"{stage}-child",
                    parent_run_id="triton-softmax-001",
                    created_at=NOW,
                    workspace_root=ROOT,
                )
                child = transition_status(child, "running", updated_at=NOW)
                child = transition_status(child, "passed", updated_at=NOW)
                child_path = root / f"{stage}.json"
                docs = {("task-config" if stage == "development" else "validation-config"): {**config(), "cases": [{"id": "basic"} ]},
                        ("development-result" if stage == "development" else "analysis"): {"status": "passed", "kernel": {"sha256": "abc"}, "results": [{"case_id": "basic", "status": "passed"}]}}
                if stage == "validation":
                    docs["case-matrix"] = {"kernel": {"sha256": "abc"}, "cases": [{"id": "basic"}]}
                for name, doc in docs.items():
                    artifact = root / f"{stage}-{name}.json"
                    artifact.write_text(json.dumps(doc), encoding="utf-8")
                    child = add_artifact(child, name=name, kind=name, uri=str(artifact))
                write_manifest(child_path, child)
                workflow._link_evidence(output, stage=stage, child_path=child_path, updated_at=NOW)
            result = workflow._finalize_report(output, updated_at=NOW)
            self.assertEqual(result["status"], "passed")

    def test_one_call_reports_missing_evidence_without_manual_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = config()
            value.pop("schema_version")
            value.pop("run_id")
            path = root / "input.json"
            path.write_text(json.dumps(value))
            result = workflow.build_report(path, {}, output_dir=root / "report", workspace_root=ROOT)
            self.assertEqual(result["status"], "inconclusive")
            self.assertTrue(Path(result["manifest"]).is_file())

    def test_passed_string_without_artifacts_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "input.json"
            path.write_text(json.dumps(config()))
            child = new_manifest(run_type="correctness", run_id="unrelated", workspace_root=ROOT)
            child = transition_status(transition_status(child, "running"), "passed")
            child_path = root / "child.json"
            write_manifest(child_path, child)
            with self.assertRaisesRegex(workflow.WorkflowError, "validation-config artifact"):
                workflow.build_report(path, {"validation": child_path}, output_dir=root / "report")

    def test_missing_stage_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "workflow"
            workflow._prepare_report(output, config_path=config_path, created_at=NOW)
            result = workflow._finalize_report(output, updated_at=NOW)
            self.assertEqual(result["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
