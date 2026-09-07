"""Unit tests for the dump comparator.

The scan and diff paths are the ones that produce most conclusions, and they
are pure JSON, so they are tested end to end through the CLI. The tensor
metrics are tested directly because they are the part that decides whether a
run is called aligned.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dump_compare.py"
_spec = importlib.util.spec_from_file_location("dump_compare_under_test", SCRIPT)
assert _spec and _spec.loader
dump_compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dump_compare)


def summary(**overrides: float) -> dict[str, float]:
    base = {
        "nan_count": 0.0,
        "inf_count": 0.0,
        "max_abs": 1.0,
        "min": -1.0,
        "max": 1.0,
        "mean": 0.0,
    }
    base.update(overrides)
    return base


def record(stage: str, order: int, **overrides) -> dict:
    entry = {
        "stage": stage,
        "order": order,
        "shape": [4, 8],
        "dtype": "torch.bfloat16",
        "numel": 32,
        "stride": [8, 1],
        "storage_offset": 0,
        "contiguous": True,
        "summary": summary(),
    }
    entry.update(overrides)
    return entry


def manifest(records: list[dict], **overrides) -> dict:
    payload = {
        "probe": "ascend-tensor-dump/1",
        "label": "req-A",
        "rank": 0,
        "match_index": 1,
        "occurrence": 1,
        "metadata": {},
        "stats": list(dump_compare.STAT_NAMES),
        "records": records,
        "graph_slots": {},
    }
    payload.update(overrides)
    return payload


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_cli(self, argv: list[str]) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dump_compare.main(argv)
        return code, json.loads(out.getvalue())


class KeyTests(unittest.TestCase):
    def test_repeated_stages_get_distinct_occurrence_keys(self) -> None:
        keys = [
            key
            for key, _ in dump_compare.record_keys(
                [record("attn", 0), record("mlp", 1), record("attn", 2)]
            )
        ]
        self.assertEqual(keys, ["attn#0", "mlp#0", "attn#1"])


class ScanTests(TempCase):
    def test_reports_first_non_finite_stage(self) -> None:
        path = self.write(
            "a.json",
            manifest(
                [
                    record("layers.0.attn", 0),
                    record("layers.23.kv", 1, summary=summary(nan_count=448.0)),
                    record("layers.24.attn", 2, summary=summary(nan_count=4096.0)),
                ]
            ),
        )
        code, payload = self.run_cli(["scan", "--manifest", str(path)])
        self.assertEqual(code, 0)
        first = payload["manifests"][0]["first_nonfinite"]
        self.assertEqual(first["stage"], "layers.23.kv")
        self.assertEqual(first["nan_count"], 448.0)
        self.assertEqual(payload["flagged"], [str(path)])

    def test_clean_manifest_is_not_flagged(self) -> None:
        path = self.write("a.json", manifest([record("attn", 0)]))
        _, payload = self.run_cli(["scan", "--manifest", str(path)])
        self.assertIsNone(payload["manifests"][0]["first_nonfinite"])
        self.assertEqual(payload["flagged"], [])

    def test_max_abs_limit_is_opt_in(self) -> None:
        path = self.write(
            "a.json", manifest([record("attn", 0, summary=summary(max_abs=1e9))])
        )
        _, without = self.run_cli(["scan", "--manifest", str(path)])
        self.assertIsNone(without["manifests"][0]["first_over_limit"])
        _, with_limit = self.run_cli(
            ["scan", "--manifest", str(path), "--max-abs-limit", "1000"]
        )
        self.assertEqual(with_limit["manifests"][0]["first_over_limit"]["max_abs"], 1e9)

    def test_stages_missing_a_summary_are_listed(self) -> None:
        clean = record("attn", 0)
        bare = record("mlp", 1)
        bare.pop("summary")
        path = self.write("a.json", manifest([clean, bare]))
        _, payload = self.run_cli(["scan", "--manifest", str(path)])
        self.assertEqual(payload["manifests"][0]["records_without_summary"], ["mlp#0"])

    def test_input_records_are_not_reported_as_missing_summary(self) -> None:
        entry = record("gmm1", 0, inputs={"hidden": {"shape": [4, 8]}})
        entry.pop("summary")
        path = self.write("a.json", manifest([entry]))
        _, payload = self.run_cli(["scan", "--manifest", str(path)])
        self.assertEqual(payload["manifests"][0]["records_without_summary"], [])

    def test_shared_storage_with_conflicting_strides_is_flagged(self) -> None:
        path = self.write(
            "a.json",
            manifest(
                [
                    record("kv_group_a", 0, storage_ptr=999, stride=[8, 1]),
                    record("kv_group_b", 1, storage_ptr=999, stride=[16, 1]),
                    record("other", 2, storage_ptr=111),
                ]
            ),
        )
        _, payload = self.run_cli(["scan", "--manifest", str(path)])
        aliases = payload["manifests"][0]["storage_aliases"]
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["storage_ptr"], 999)
        self.assertEqual(aliases[0]["stages"], ["kv_group_a", "kv_group_b"])
        self.assertTrue(aliases[0]["stride_conflict"])

    def test_plain_views_share_storage_without_a_stride_conflict(self) -> None:
        path = self.write(
            "a.json",
            manifest(
                [
                    record("qkv", 0, storage_ptr=777),
                    record("qkv_view", 1, storage_ptr=777),
                ]
            ),
        )
        _, payload = self.run_cli(["scan", "--manifest", str(path)])
        self.assertFalse(payload["manifests"][0]["storage_aliases"][0]["stride_conflict"])

    def test_single_stage_reuse_of_one_allocation_is_not_an_alias(self) -> None:
        path = self.write(
            "a.json",
            manifest(
                [
                    record("attn", 0, storage_ptr=555),
                    record("attn", 1, storage_ptr=555),
                ]
            ),
        )
        _, payload = self.run_cli(["scan", "--manifest", str(path)])
        self.assertEqual(payload["manifests"][0]["storage_aliases"], [])

    def test_missing_pointer_is_ignored(self) -> None:
        path = self.write(
            "a.json", manifest([record("a", 0, storage_ptr=0), record("b", 1)])
        )
        _, payload = self.run_cli(["scan", "--manifest", str(path)])
        self.assertEqual(payload["manifests"][0]["storage_aliases"], [])

    def test_missing_file_reports_a_failed_status(self) -> None:
        code, payload = self.run_cli(
            ["scan", "--manifest", str(self.root / "nope.json")]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "failed")

    def test_manifest_without_records_is_rejected(self) -> None:
        path = self.root / "bad.json"
        path.write_text(json.dumps({"probe": "x"}), encoding="utf-8")
        code, payload = self.run_cli(["scan", "--manifest", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("records", payload["error"])


class DiffTests(TempCase):
    def test_identical_manifests_are_aligned(self) -> None:
        records = [record("attn", 0), record("mlp", 1)]
        left = self.write("l.json", manifest(records))
        right = self.write("r.json", manifest(records))
        code, payload = self.run_cli(
            ["diff", "--left", str(left), "--right", str(right)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["verdict"], "ALIGNED")
        self.assertIsNone(payload["first_divergent"])
        self.assertEqual(payload["compared"], 2)

    def test_first_divergent_stage_is_the_earliest_one(self) -> None:
        left = self.write(
            "l.json", manifest([record("a", 0), record("b", 1), record("c", 2)])
        )
        right = self.write(
            "r.json",
            manifest(
                [
                    record("a", 0),
                    record("b", 1, summary=summary(max_abs=2.0)),
                    record("c", 2, summary=summary(max_abs=9.0)),
                ]
            ),
        )
        _, payload = self.run_cli(["diff", "--left", str(left), "--right", str(right)])
        self.assertEqual(payload["first_divergent"]["stage"], "b")
        self.assertEqual(payload["first_divergent"]["reasons"], ["statistics"])
        self.assertEqual(payload["verdict"], "DIVERGENT")

    def test_tolerance_absorbs_small_statistic_drift(self) -> None:
        left = self.write("l.json", manifest([record("a", 0)]))
        right = self.write(
            "r.json", manifest([record("a", 0, summary=summary(max_abs=1.0001))])
        )
        _, strict = self.run_cli(["diff", "--left", str(left), "--right", str(right)])
        self.assertEqual(strict["verdict"], "DIVERGENT")
        _, loose = self.run_cli(
            [
                "diff",
                "--left",
                str(left),
                "--right",
                str(right),
                "--atol",
                "1e-3",
            ]
        )
        self.assertEqual(loose["verdict"], "ALIGNED")

    def test_shape_mismatch_is_reported_as_its_own_reason(self) -> None:
        left = self.write("l.json", manifest([record("a", 0, shape=[4, 8])]))
        right = self.write("r.json", manifest([record("a", 0, shape=[8, 8])]))
        _, payload = self.run_cli(["diff", "--left", str(left), "--right", str(right)])
        self.assertIn("shape", payload["first_divergent"]["reasons"])
        self.assertFalse(payload["stages"][0]["shape_match"])

    def test_introduced_non_finite_values_are_called_out(self) -> None:
        left = self.write("l.json", manifest([record("a", 0)]))
        right = self.write(
            "r.json", manifest([record("a", 0, summary=summary(nan_count=8.0))])
        )
        _, payload = self.run_cli(["diff", "--left", str(left), "--right", str(right)])
        self.assertIn("nonfinite", payload["first_divergent"]["reasons"])

    def test_matching_nan_on_both_sides_is_not_divergence(self) -> None:
        both = manifest([record("a", 0, summary=summary(nan_count=8.0))])
        left = self.write("l.json", both)
        right = self.write("r.json", both)
        _, payload = self.run_cli(["diff", "--left", str(left), "--right", str(right)])
        self.assertEqual(payload["verdict"], "ALIGNED")

    def test_unpaired_stages_are_listed_rather_than_compared(self) -> None:
        left = self.write("l.json", manifest([record("a", 0), record("only-l", 1)]))
        right = self.write("r.json", manifest([record("a", 0), record("only-r", 1)]))
        _, payload = self.run_cli(["diff", "--left", str(left), "--right", str(right)])
        self.assertEqual(payload["only_in_left"], ["only-l#0"])
        self.assertEqual(payload["only_in_right"], ["only-r#0"])
        self.assertEqual(payload["compared"], 1)

    def test_fail_on_divergence_changes_the_exit_code(self) -> None:
        left = self.write("l.json", manifest([record("a", 0)]))
        right = self.write(
            "r.json", manifest([record("a", 0, summary=summary(max_abs=5.0))])
        )
        base, _ = self.run_cli(["diff", "--left", str(left), "--right", str(right)])
        self.assertEqual(base, 0)
        gated, _ = self.run_cli(
            [
                "diff",
                "--left",
                str(left),
                "--right",
                str(right),
                "--fail-on-divergence",
            ]
        )
        self.assertEqual(gated, 1)


class MetricTests(unittest.TestCase):
    def test_identical_float_tensors(self) -> None:
        metrics = dump_compare.tensor_metrics(
            [1.0, 2.0, 3.0], [1.0, 2.0, 3.0], integral=False, atol=0.0, rtol=0.0
        )
        self.assertTrue(metrics["exact_equal"])
        self.assertTrue(metrics["allclose"])
        self.assertEqual(metrics["max_abs_diff"], 0.0)
        self.assertAlmostEqual(metrics["cosine"], 1.0)
        self.assertEqual(metrics["rel_l2"], 0.0)

    def test_metrics_capture_the_size_of_the_difference(self) -> None:
        metrics = dump_compare.tensor_metrics(
            [1.0, 2.0], [1.0, 2.5], integral=False, atol=0.0, rtol=0.0
        )
        self.assertFalse(metrics["exact_equal"])
        self.assertEqual(metrics["mismatch_count"], 1)
        self.assertAlmostEqual(metrics["max_abs_diff"], 0.5)
        self.assertAlmostEqual(metrics["mean_abs_diff"], 0.25)

    def test_tolerance_is_applied_relative_to_the_right_side(self) -> None:
        loose = dump_compare.tensor_metrics(
            [1.0], [1.005], integral=False, atol=0.0, rtol=1e-2
        )
        self.assertTrue(loose["allclose"])
        tight = dump_compare.tensor_metrics(
            [1.0], [1.005], integral=False, atol=0.0, rtol=1e-4
        )
        self.assertFalse(tight["allclose"])

    def test_non_finite_values_make_the_pair_incomparable(self) -> None:
        metrics = dump_compare.tensor_metrics(
            [1.0, float("nan")], [1.0, 2.0], integral=False, atol=0.0, rtol=0.0
        )
        self.assertFalse(metrics["comparable"])
        self.assertEqual(metrics["left_nonfinite"], 1)
        self.assertEqual(metrics["right_nonfinite"], 0)
        self.assertNotIn("max_abs_diff", metrics)

    def test_integral_tensors_only_get_a_mismatch_count(self) -> None:
        metrics = dump_compare.tensor_metrics(
            [1.0, 2.0, 3.0], [1.0, 9.0, 3.0], integral=True, atol=0.0, rtol=0.0
        )
        self.assertEqual(metrics["mismatch_count"], 1)
        self.assertNotIn("cosine", metrics)
        self.assertNotIn("allclose", metrics)

    def test_zero_norm_side_does_not_produce_a_cosine(self) -> None:
        metrics = dump_compare.tensor_metrics(
            [0.0, 0.0], [0.0, 0.0], integral=False, atol=0.0, rtol=0.0
        )
        self.assertNotIn("cosine", metrics)
        self.assertTrue(metrics["exact_equal"])


class PayloadTests(unittest.TestCase):
    def test_all_three_sources_are_flattened_into_one_key_space(self) -> None:
        payload = {
            "tensors": [("attn", "T1"), ("attn", "T2")],
            "inputs": [("gmm1", {"hidden": "T3", "group_list_type": 1})],
            "graph_slots": {"layers.0.out": "T4"},
        }
        flat = dump_compare.flatten_payload(payload, label="left")
        self.assertEqual(
            sorted(flat),
            [
                "attn#0",
                "attn#1",
                "gmm1:group_list_type#0",
                "gmm1:hidden#0",
                "graph:layers.0.out#0",
            ],
        )
        self.assertEqual(flat["attn#1"], "T2")

    def test_missing_sections_are_tolerated(self) -> None:
        self.assertEqual(dump_compare.flatten_payload({}, label="left"), {})

    def test_non_dict_payload_is_rejected(self) -> None:
        with self.assertRaises(dump_compare.DumpCompareError):
            dump_compare.flatten_payload(["not", "a", "dict"], label="left")


if __name__ == "__main__":
    unittest.main()
