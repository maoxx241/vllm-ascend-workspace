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


def init_case(case_dir: Path, case_id: str = "graph-case-1", **overrides) -> dict:
    arguments = {
        "case_id": case_id,
        "stage": "accuracy",
        "eager_result": "pass",
        "graph_result": "fail",
        "reproduction": "fixed input",
        "created_at": NOW,
    }
    arguments.update(overrides)
    return graph_debug.init_case(case_dir, **arguments)


def record_one_experiment(case_dir: Path) -> None:
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


def write_evidence(root: Path) -> tuple[Path, Path]:
    minimal = root / "minimal-rerun.log"
    original = root / "original-rerun.log"
    minimal.write_text("minimal reproduction: 32/32 tokens match\n", encoding="utf-8")
    original.write_text("original reproduction: exact match\n", encoding="utf-8")
    return minimal, original


class GraphDebugCaseTests(unittest.TestCase):
    def test_case_lifecycle_resolves_only_after_both_reproductions_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(case_dir, parent_run_id="change-validation-1")
            record_one_experiment(case_dir)
            minimal, original = write_evidence(Path(tmp))
            case = graph_debug.finalize_case(
                case_dir,
                root_cause="stale metadata",
                fix="refresh fixed buffer",
                minimal_result="pass",
                original_result="pass",
                cleanup_status="removed",
                minimal_evidence=minimal,
                original_evidence=original,
                updated_at=NOW,
            )
            self.assertEqual(case["status"], "resolved")
            self.assertEqual(case["resolution"]["experiment_count"], 1)
            manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "passed")
            self.assertEqual(manifest["parent_run_id"], "change-validation-1")
            artifacts = {row["name"]: row for row in manifest["artifacts"]}
            self.assertIn("graph-debug-case", artifacts)
            for name in ("validation-minimal-reproduction", "validation-original-reproduction"):
                self.assertIn(name, artifacts)
                self.assertNotIn("sha256", artifacts[name])
                self.assertTrue((case_dir / artifacts[name]["uri"]).is_file())

    def test_init_then_finalize_cannot_reach_passed(self) -> None:
        """Regression: two commands and zero evidence used to yield `passed`."""
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(case_dir, stage="unknown")
            with self.assertRaisesRegex(
                graph_debug.GraphDebugError,
                r"--minimal-evidence.*--original-evidence.*no controlled experiment",
            ):
                graph_debug.finalize_case(
                    case_dir,
                    root_cause="x",
                    fix="y",
                    minimal_result="pass",
                    original_result="pass",
                    cleanup_status="removed",
                    updated_at=NOW,
                )
            manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "planned")
            case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
            self.assertEqual(case["status"], "active")

    def test_pass_claim_without_experiments_is_rejected_even_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(case_dir)
            minimal, original = write_evidence(Path(tmp))
            with self.assertRaisesRegex(
                graph_debug.GraphDebugError, "no controlled experiment recorded"
            ):
                graph_debug.finalize_case(
                    case_dir,
                    root_cause="x",
                    fix="y",
                    minimal_result="pass",
                    original_result="pass",
                    cleanup_status="removed",
                    minimal_evidence=minimal,
                    original_evidence=original,
                    updated_at=NOW,
                )

    def test_empty_evidence_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(case_dir)
            record_one_experiment(case_dir)
            minimal, original = write_evidence(Path(tmp))
            original.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(
                graph_debug.GraphDebugError, "--original-evidence is empty"
            ):
                graph_debug.finalize_case(
                    case_dir,
                    root_cause="x",
                    fix="y",
                    minimal_result="pass",
                    original_result="pass",
                    cleanup_status="removed",
                    minimal_evidence=minimal,
                    original_evidence=original,
                    updated_at=NOW,
                )

    def test_failed_reproduction_can_close_inconclusive_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(case_dir, case_id="graph-case-3", stage="replay")
            case = graph_debug.finalize_case(
                case_dir,
                root_cause="event mismatch",
                fix="pair replay events",
                minimal_result="pass",
                original_result="fail",
                cleanup_status="removed",
                minimal_evidence=write_evidence(Path(tmp))[0],
                updated_at=NOW,
            )
            self.assertEqual(case["status"], "inconclusive")
            manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "inconclusive")

    def test_pending_cleanup_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(case_dir, case_id="graph-case-2", stage="replay")
            record_one_experiment(case_dir)
            minimal, original = write_evidence(Path(tmp))
            case = graph_debug.finalize_case(
                case_dir,
                root_cause="event mismatch",
                fix="pair replay events",
                minimal_result="pass",
                original_result="pass",
                cleanup_status="pending",
                minimal_evidence=minimal,
                original_evidence=original,
                updated_at=NOW,
            )
            self.assertEqual(case["status"], "inconclusive")

    def test_cli_finalize_without_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(case_dir)
            record_one_experiment(case_dir)
            exit_code = graph_debug.main(
                [
                    "finalize",
                    "--case-dir",
                    str(case_dir),
                    "--root-cause",
                    "x",
                    "--fix",
                    "y",
                    "--minimal-result",
                    "pass",
                    "--original-result",
                    "pass",
                    "--cleanup-status",
                    "removed",
                ]
            )
            self.assertEqual(exit_code, 1)
            manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotEqual(manifest["status"], "passed")

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
            init_case(case_dir)
            eager = Path(tmp) / "eager.jsonl"
            graph = Path(tmp) / "graph.jsonl"
            write_snapshot(eager, [record(layer=0, sample=[1.0])])
            write_snapshot(graph, [record(layer=0, sample=[1.0])])
            with self.assertRaisesRegex(
                graph_debug.GraphDebugError, "no recorded identity"
            ):
                graph_debug.compare_case(
                    case_dir,
                    eager_path=eager,
                    graph_path=graph,
                    atol=0.0,
                    rtol=0.0,
                    updated_at=NOW,
                )

    def test_compare_case_consumes_comparable_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(case_dir)
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
                json.dumps(identity), encoding="utf-8"
            )
            (Path(tmp) / "graph.identity.json").write_text(
                json.dumps(identity), encoding="utf-8"
            )
            comparison, output = graph_debug.compare_case(
                case_dir,
                eager_path=eager,
                graph_path=graph,
                atol=0.0,
                rtol=0.0,
                updated_at=NOW,
            )
            self.assertEqual(comparison["status"], "exact-match")
            self.assertEqual(comparison["comparability"]["verdict"], "comparable")
            self.assertTrue(output.is_file())

    def test_declaration_mismatch_cannot_compare_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "case"
            init_case(
                case_dir,
                topology={"tp": 8, "dp": 1},
                workspace_snapshot={"vllm_ascend_commit": "aaaa1111"},
                environment={"cann": "test"},
                model={"path": "/models/example"},
            )
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
                json.dumps(identity), encoding="utf-8"
            )
            (Path(tmp) / "graph.identity.json").write_text(
                json.dumps(identity), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                graph_debug.GraphDebugError, "declaration/observation mismatch"
            ):
                graph_debug.compare_case(
                    case_dir,
                    eager_path=eager,
                    graph_path=graph,
                    atol=0.0,
                    rtol=0.0,
                    updated_at=NOW,
                )
            self.assertFalse((case_dir / "comparisons").exists())

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
