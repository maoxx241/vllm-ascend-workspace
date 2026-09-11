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


class GraphCompareTests(unittest.TestCase):
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

    def test_compare_case_requires_recorded_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            eager = Path(tmp) / "eager.jsonl"
            graph = Path(tmp) / "graph.jsonl"
            write_snapshot(eager, [record(layer=0, sample=[1.0])])
            write_snapshot(graph, [record(layer=0, sample=[1.0])])
            with self.assertRaisesRegex(
                graph_debug.GraphDebugError, "no recorded identity"
            ):
                graph_debug.build_report(eager, graph, output_dir=case_dir,
                    atol=0.0,
                    rtol=0.0,
                )

    def test_compare_case_consumes_comparable_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            eager = Path(tmp) / "eager.jsonl"
            graph = Path(tmp) / "graph.jsonl"
            write_snapshot(eager, [record(layer=0, sample=[1.0])])
            write_snapshot(graph, [record(layer=0, sample=[1.0])])
            identity = {
                "workspace_snapshot": {"vllm_ascend_commit": "aaaa1111"},
                "environment": {"cann": "test"},
                "model": {"path": "/models/example"},
                "topology": {"tp": 2, "dp": 1},
                "native_digest": "cd" * 32,
            }
            (Path(tmp) / "eager.identity.json").write_text(
                json.dumps({**identity, "execution_mode": "eager"}), encoding="utf-8"
            )
            (Path(tmp) / "graph.identity.json").write_text(
                json.dumps({**identity, "execution_mode": "graph"}), encoding="utf-8"
            )
            result = graph_debug.build_report(eager, graph, output_dir=case_dir,
                atol=0.0,
                rtol=0.0,
            )
            comparison = json.loads(Path(result["comparison"]).read_text(encoding="utf-8"))
            self.assertEqual(comparison["status"], "exact-match")
            self.assertEqual(comparison["comparability"]["verdict"], "comparable")
            self.assertTrue(Path(result["manifest_ref"]).is_file())

    def test_empty_snapshots_are_not_agreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            eager, graph = Path(tmp)/"eager.jsonl", Path(tmp)/"graph.jsonl"
            write_snapshot(eager, [])
            write_snapshot(graph, [])
            with self.assertRaisesRegex(graph_debug.GraphDebugError, "no records"):
                graph_debug.compare_snapshots(eager, graph, atol=0, rtol=0)

    def test_nonfinite_tolerance_is_rejected(self):
        with self.assertRaisesRegex(graph_debug.GraphDebugError, "finite"):
            graph_debug.compare_snapshots(Path("missing"), Path("missing"), atol=float("inf"), rtol=0)

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
