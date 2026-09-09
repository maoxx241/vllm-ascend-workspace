#!/usr/bin/env python3
"""Tests for change impact planning and evidence aggregation."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "vllm-ascend-change-validation"
KNOWLEDGE = SKILL / "references" / "validation-rules.yaml"
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_coordinator.run_manifest import (  # noqa: E402
    add_artifact,
    new_manifest,
    transition_status,
    write_manifest,
)


def load_module():
    name = "_change_validation_test"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / "change_validation.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


change_validation = load_module()
NOW = "2026-07-25T12:00:00Z"


def unified_diff(path: str, added: str = "changed") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        f"+{added}\n"
    )


class PlanningTests(unittest.TestCase):
    def test_graph_change_requires_eager_and_graph(self) -> None:
        knowledge = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
        summary = change_validation.parse_diff(
            unified_diff("vllm_ascend/graph/acl_graph.py")
        )
        impact, plan = change_validation.build_plan(summary, knowledge)
        checks = {item["check"] for item in plan["items"]}
        self.assertIn("correctness:eager", checks)
        self.assertIn("correctness:graph", checks)
        self.assertIn("graph", {row["category"] for row in impact["impacts"]})

    def test_unknown_change_gets_required_fallback(self) -> None:
        knowledge = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
        summary = change_validation.parse_diff(unified_diff("docs/niche_note.md"))
        _impact, plan = change_validation.build_plan(summary, knowledge)
        self.assertTrue(plan["manual_review_required"])
        self.assertEqual(plan["items"][0]["check"], "correctness:targeted-smoke")
        self.assertEqual(plan["items"][0]["priority"], "required")

    def test_duplicate_checks_merge_and_required_wins(self) -> None:
        knowledge = {
            "entries": [
                {
                    "id": "one",
                    "status": "active",
                    "rule": {
                        "category": "a",
                        "path_patterns": ["target"],
                        "recommended_checks": ["correctness:smoke"],
                    },
                },
                {
                    "id": "two",
                    "status": "active",
                    "rule": {
                        "category": "b",
                        "path_patterns": ["target"],
                        "required_checks": ["correctness:smoke"],
                    },
                },
            ]
        }
        summary = change_validation.parse_diff(unified_diff("target.py"))
        _impact, plan = change_validation.build_plan(summary, knowledge)
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["items"][0]["priority"], "required")
        self.assertEqual(plan["items"][0]["sources"], ["one", "two"])

    def test_path_keywords_in_changed_document_text_do_not_trigger(self) -> None:
        knowledge = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
        summary = change_validation.parse_diff(
            unified_diff("docs/note.md", added="graph worker quantization scheduler")
        )
        impact, plan = change_validation.build_plan(summary, knowledge)
        self.assertEqual(impact["impacts"], [])
        self.assertTrue(plan["manual_review_required"])


def plan_graph_change(output: Path, run_id: str = "change-validation-1") -> list[str]:
    change_validation.plan_change(
        output,
        run_id=run_id,
        baseline="base",
        candidate="candidate",
        goal="graph fix",
        target_repositories=["vllm-ascend"],
        diff_text=unified_diff("graph_mode.py"),
        knowledge_path=KNOWLEDGE,
        created_at=NOW,
    )
    plan = json.loads((output / "validation-plan.json").read_text(encoding="utf-8"))
    return [item["id"] for item in plan["items"] if item["priority"] == "required"]


def passed_child(
    path: Path,
    *,
    parent_run_id: str | None,
    with_artifact: bool = True,
    run_type: str = "correctness",
) -> None:
    child = new_manifest(
        run_type=run_type,
        run_id="child-1",
        parent_run_id=parent_run_id,
        created_at=NOW,
        code={"source_head": "a" * 40, "snapshot_commit": "b" * 40, "dirty": False},
    )
    child = transition_status(child, "running", updated_at=NOW)
    if with_artifact:
        child = add_artifact(
            child, name="comparison", kind="comparison", uri="comparison.json", updated_at=NOW
        )
    child = transition_status(child, "passed", updated_at=NOW)
    write_manifest(path, child)


class AggregationTests(unittest.TestCase):
    def test_passed_child_can_complete_required_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "change"
            required_ids = plan_graph_change(output)
            child_path = root / "child-manifest.json"
            passed_child(child_path, parent_run_id="change-validation-1")
            change_validation.link_run(
                output,
                child_manifest_path=child_path,
                covers=required_ids,
                updated_at=NOW,
            )
            result = change_validation.finalize(output, updated_at=NOW)
            self.assertEqual(result["status"], "passed")
            report = (output / "pr-validation-report.md").read_text(encoding="utf-8")
            self.assertIn("child-1", report)

    def test_child_without_parent_run_id_is_rejected(self) -> None:
        """Regression: a null parent_run_id used to satisfy the parent check."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "change"
            required_ids = plan_graph_change(output)
            child_path = root / "orphan.json"
            passed_child(child_path, parent_run_id=None)
            with self.assertRaisesRegex(
                change_validation.ChangeValidationError,
                "has no parent_run_id.*--parent-run-id",
            ):
                change_validation.link_run(
                    output,
                    child_manifest_path=child_path,
                    covers=required_ids,
                    updated_at=NOW,
                )
            links = json.loads((output / "linked-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(links["runs"], [])
            result = change_validation.finalize(output, updated_at=NOW)
            self.assertEqual(result["status"], "inconclusive")

    def test_child_of_another_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "change"
            required_ids = plan_graph_change(output)
            child_path = root / "foreign.json"
            passed_child(child_path, parent_run_id="change-validation-other")
            with self.assertRaisesRegex(
                change_validation.ChangeValidationError, "belongs to parent"
            ):
                change_validation.link_run(
                    output,
                    child_manifest_path=child_path,
                    covers=required_ids,
                    updated_at=NOW,
                )

    def test_passed_child_without_artifacts_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "change"
            required_ids = plan_graph_change(output)
            child_path = root / "bare.json"
            passed_child(
                child_path, parent_run_id="change-validation-1", with_artifact=False
            )
            with self.assertRaisesRegex(
                change_validation.ChangeValidationError, "links no artifacts"
            ):
                change_validation.link_run(
                    output,
                    child_manifest_path=child_path,
                    covers=required_ids,
                    updated_at=NOW,
                )

    def test_debug_manifest_cannot_cover_correctness_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "change"
            required_ids = plan_graph_change(output)
            child_path = root / "debug-child.json"
            passed_child(
                child_path,
                parent_run_id="change-validation-1",
                run_type="debug",
            )
            with self.assertRaisesRegex(
                change_validation.ChangeValidationError,
                r"run_type 'debug'.*requires run_type 'correctness'",
            ):
                change_validation.link_run(
                    output,
                    child_manifest_path=child_path,
                    covers=required_ids,
                    updated_at=NOW,
                )
            links = json.loads((output / "linked-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(links["runs"], [])

    def test_missing_required_evidence_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "change"
            change_validation.plan_change(
                output,
                run_id="change-validation-2",
                baseline="base",
                candidate="candidate",
                goal="graph fix",
                target_repositories=["vllm-ascend"],
                diff_text=unified_diff("graph_mode.py"),
                knowledge_path=KNOWLEDGE,
                created_at=NOW,
            )
            result = change_validation.finalize(output, updated_at=NOW)
            self.assertEqual(result["status"], "inconclusive")
            self.assertTrue(result["missing_required"])


if __name__ == "__main__":
    unittest.main()
