#!/usr/bin/env python3
"""Tests for controlled A/B performance regression analysis."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".agents" / "skills" / "vllm-ascend-performance-regression"


def load_module():
    name = "_performance_regression_test"
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / "performance_regression.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


performance = load_module()
NOW = "2026-07-25T12:00:00Z"


def config() -> dict:
    return {
        "schema_version": 1,
        "run_id": "performance-case-1",
        "baseline": {
            "label": "baseline",
            "code_snapshot": "abc",
            "session_id": "base-session",
        },
        "candidate": {
            "label": "candidate",
            "code_snapshot": "def",
            "session_id": "candidate-session",
        },
        "shared": {
            "machine": "host",
            "npu_devices": [0, 1],
            "model": {"path": "/models/example"},
            "environment": {"cann": "test"},
            "topology": {"tp": 2},
            "serve_args": [],
            "bench_args": [],
            "dataset": "test",
        },
        "warmups": 1,
        "runs": 3,
        "max_cv": 0.1,
        "exclude_outliers": False,
        "thresholds": {
            "throughput": {
                "direction": "higher",
                "max_relative_regression": 0.03,
            },
            "ttft": {
                "direction": "lower",
                "max_relative_regression": 0.05,
            },
        },
    }


def measurement(
    entry: dict, fingerprint: str, *, throughput: float, ttft: float
) -> dict:
    return {
        "schema_version": 1,
        "state": entry["state"],
        "phase": entry["phase"],
        "ordinal": entry["ordinal"],
        "config_hash": fingerprint,
        "metrics": {"throughput": throughput, "ttft": ttft},
    }


class PerformanceRegressionTests(unittest.TestCase):
    def test_normalizes_single_run_benchmark_result(self) -> None:
        result = performance.normalize_benchmark_result(
            {
                "status": "ok",
                "metrics": {
                    "output_throughput": 123.0,
                    "mean_ttft_ms": 45.0,
                    "ignored": "value",
                },
            },
            state="baseline",
            phase="measure",
            ordinal=1,
            fingerprint="a" * 64,
            source="/tmp/bench.json",
        )
        self.assertEqual(result["metrics"], {"throughput": 123.0, "ttft": 45.0})

    def test_normalizes_aggregated_benchmark_result(self) -> None:
        result = performance.normalize_benchmark_result(
            {
                "status": "ok",
                "aggregated": {
                    "output_throughput": {"mean": 120.0, "stddev": 2.0},
                    "mean_tpot_ms": {"mean": 4.5, "stddev": 0.1},
                },
            },
            state="candidate",
            phase="warmup",
            ordinal=1,
            fingerprint="b" * 64,
            source="/tmp/bench.json",
        )
        self.assertEqual(result["metrics"], {"throughput": 120.0, "tpot": 4.5})

    def test_three_run_schedule_alternates(self) -> None:
        schedule = performance.build_schedule(warmups=1, runs=3)
        self.assertEqual(
            [entry["id"] for entry in schedule],
            [
                "baseline-warmup-1",
                "candidate-warmup-1",
                "baseline-measure-1",
                "candidate-measure-1",
                "candidate-measure-2",
                "baseline-measure-2",
                "baseline-measure-3",
                "candidate-measure-3",
            ],
        )

    def test_same_session_is_rejected(self) -> None:
        invalid = config()
        invalid["candidate"]["session_id"] = invalid["baseline"]["session_id"]
        with self.assertRaisesRegex(performance.PerformanceRegressionError, "different"):
            performance.validate_config(invalid)

    def test_regression_is_detected(self) -> None:
        experiment = config()
        schedule = {
            "config_hash": performance.config_hash(experiment["shared"]),
            "entries": performance.build_schedule(warmups=1, runs=3),
        }
        for entry in schedule["entries"]:
            entry["status"] = "recorded"
        rows = []
        for entry in schedule["entries"]:
            rows.append(
                {
                    **measurement(
                        entry,
                        schedule["config_hash"],
                        throughput=100.0 if entry["state"] == "baseline" else 90.0,
                        ttft=10.0 if entry["state"] == "baseline" else 11.0,
                    ),
                    "schedule_id": entry["id"],
                }
            )
        comparison = performance.analyze_documents(
            experiment,
            schedule,
            {"measurements": rows},
        )
        self.assertEqual(comparison["status"], "failed")
        self.assertEqual(comparison["regressions"], ["throughput", "ttft"])

    def test_high_variation_is_inconclusive(self) -> None:
        experiment = config()
        schedule = {
            "config_hash": performance.config_hash(experiment["shared"]),
            "entries": performance.build_schedule(warmups=1, runs=3),
        }
        for entry in schedule["entries"]:
            entry["status"] = "recorded"
        values = {
            ("baseline", 1): 100.0,
            ("baseline", 2): 50.0,
            ("baseline", 3): 150.0,
            ("candidate", 1): 100.0,
            ("candidate", 2): 100.0,
            ("candidate", 3): 100.0,
        }
        rows = []
        for entry in schedule["entries"]:
            value = values.get((entry["state"], entry["ordinal"]), 100.0)
            rows.append(
                {
                    **measurement(
                        entry,
                        schedule["config_hash"],
                        throughput=value,
                        ttft=10.0,
                    ),
                    "schedule_id": entry["id"],
                }
            )
        comparison = performance.analyze_documents(
            experiment, schedule, {"measurements": rows}
        )
        self.assertEqual(comparison["status"], "inconclusive")
        self.assertIn("throughput", comparison["noisy_metrics"])

    def test_full_lifecycle_records_in_schedule_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "run"
            planned = performance.plan(output, config_path=config_path, created_at=NOW)
            schedule = json.loads(
                (output / "schedule.json").read_text(encoding="utf-8")
            )
            for entry in schedule["entries"]:
                result_path = root / f"{entry['id']}.json"
                result_path.write_text(
                    json.dumps(
                        measurement(
                            entry,
                            planned["config_hash"],
                            throughput=100.0,
                            ttft=10.0,
                        )
                    ),
                    encoding="utf-8",
                )
                performance.record(output, result_path=result_path, recorded_at=NOW)
            result = performance.analyze(output, updated_at=NOW)
            self.assertEqual(result["status"], "passed")
            self.assertTrue((output / "report.md").is_file())

    def test_wrong_config_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            output = root / "run"
            performance.plan(output, config_path=config_path, created_at=NOW)
            entry = json.loads(
                (output / "schedule.json").read_text(encoding="utf-8")
            )["entries"][0]
            result_path = root / "wrong.json"
            result_path.write_text(
                json.dumps(measurement(entry, "0" * 64, throughput=1.0, ttft=1.0)),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                performance.PerformanceRegressionError, "config_hash"
            ):
                performance.record(output, result_path=result_path, recorded_at=NOW)


if __name__ == "__main__":
    unittest.main()
