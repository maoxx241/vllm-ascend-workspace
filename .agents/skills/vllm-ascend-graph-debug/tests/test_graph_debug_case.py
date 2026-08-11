#!/usr/bin/env python3
"""Tests for the graph-debug case controller."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = (
    ROOT
    / ".agents"
    / "skills"
    / "vllm-ascend-graph-debug"
    / "scripts"
    / "graph_debug_case.py"
)


def load_module():
    module_name = "_graph_debug_case_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


graph_debug = load_module()
NOW = "2026-07-25T12:00:00Z"


def write_snapshot(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def record(*, layer: int, sample: list[float]) -> dict:
    return {
        "step": 0,
        "layer": layer,
        "rank": 0,
        "tag": "out",
        "stats": {"mean": sum(sample) / len(sample)},
        "sample": sample,
    }


class GraphDebugCaseTests(unittest.TestCase):
    def test_case_lifecycle_resolves_only_after_both_reproductions_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            graph_debug.init_case(
                case_dir,
                case_id="graph-case-1",
                stage="accuracy",
                eager_result="pass",
                graph_result="fail",
                reproduction="fixed input",
                created_at=NOW,
            )
            graph_debug.record_experiment(
                case_dir,
                variable="capture size",
                hypothesis="padding causes divergence",
                expected="smaller capture removes divergence",
                observed="divergence remains",
                conclusion="capture size excluded",
                next_step="inspect metadata",
                updated_at=NOW,
            )
            case = graph_debug.finalize_case(
                case_dir,
                root_cause="stale metadata",
                fix="refresh fixed buffer",
                minimal_result="pass",
                original_result="pass",
                cleanup_status="removed",
                updated_at=NOW,
            )
            self.assertEqual(case["status"], "resolved")
            manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "passed")
            self.assertEqual(manifest["artifacts"][0]["name"], "graph-debug-case")

    def test_pending_cleanup_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            graph_debug.init_case(
                case_dir,
                case_id="graph-case-2",
                stage="replay",
                eager_result="pass",
                graph_result="fail",
                reproduction="fixed input",
                created_at=NOW,
            )
            case = graph_debug.finalize_case(
                case_dir,
                root_cause="event mismatch",
                fix="pair replay events",
                minimal_result="pass",
                original_result="pass",
                cleanup_status="pending",
                updated_at=NOW,
            )
            self.assertEqual(case["status"], "inconclusive")

    def test_snapshot_comparison_reports_first_sorted_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eager = root / "eager.jsonl"
            graph = root / "graph.jsonl"
            write_snapshot(
                eager,
                [record(layer=2, sample=[1.0]), record(layer=1, sample=[1.0])],
            )
            write_snapshot(
                graph,
                [record(layer=2, sample=[3.0]), record(layer=1, sample=[2.0])],
            )
            comparison = graph_debug.compare_snapshots(
                eager, graph, atol=0.0, rtol=0.0
            )
            self.assertEqual(comparison["status"], "diverged")
            self.assertEqual(comparison["first_divergence"]["key"]["layer"], 1)
            self.assertEqual(comparison["divergence_count"], 2)

    def test_tolerance_can_accept_small_numeric_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            eager = root / "eager.jsonl"
            graph = root / "graph.jsonl"
            write_snapshot(eager, [record(layer=0, sample=[1.0])])
            write_snapshot(graph, [record(layer=0, sample=[1.00001])])
            comparison = graph_debug.compare_snapshots(
                eager, graph, atol=1e-4, rtol=0.0
            )
            self.assertEqual(comparison["status"], "exact-match")

    def test_duplicate_snapshot_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.jsonl"
            write_snapshot(
                path,
                [record(layer=0, sample=[1.0]), record(layer=0, sample=[2.0])],
            )
            with self.assertRaisesRegex(graph_debug.GraphDebugError, "duplicate"):
                graph_debug.load_snapshots(path)


if __name__ == "__main__":
    unittest.main()
