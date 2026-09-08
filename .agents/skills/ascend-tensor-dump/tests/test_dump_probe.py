"""Unit tests for the probe selector and capture gating.

These run without torch on purpose. The probe is duck-typed so the logic that
decides *which* forward to arm and *what* to record can be tested on the same
machine that reviews the code, which is where the selector bugs live.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ASSET = (
    Path(__file__).resolve().parents[1] / "assets" / "dump_probe.py"
)
_spec = importlib.util.spec_from_file_location("dump_probe_under_test", ASSET)
assert _spec and _spec.loader
dump_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dump_probe)


class FakeTensor:
    """Minimal stand-in exposing only what the probe actually touches."""

    def __init__(self, *shape: int, dtype: str = "torch.bfloat16"):
        self.shape = shape or (4, 8)
        self.dtype = dtype
        self._sliced_to: int | None = None

    def detach(self) -> "FakeTensor":
        return self

    def numel(self) -> int:
        count = 1
        for dimension in self.shape:
            count *= dimension
        return count

    def dim(self) -> int:
        return len(self.shape)

    def stride(self) -> tuple[int, ...]:
        strides = [1]
        for dimension in reversed(self.shape[1:]):
            strides.append(strides[-1] * dimension)
        return tuple(reversed(strides))

    def is_contiguous(self) -> bool:
        return True

    def __getitem__(self, item) -> "FakeTensor":
        stop = item.stop if isinstance(item, slice) else item
        clone = FakeTensor(int(stop), *self.shape[1:], dtype=self.dtype)
        clone._sliced_to = int(stop)
        return clone

    def contiguous(self) -> "FakeTensor":
        return self

    def clone(self) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self


ENV_KEYS = [
    "DUMP_PROBE",
    "DUMP_PROBE_DIR",
    "DUMP_PROBE_RANKS",
    "DUMP_PROBE_MATCH",
    "DUMP_PROBE_OCCURRENCE",
    "DUMP_PROBE_SUMMARY",
    "DUMP_PROBE_TENSOR",
    "DUMP_PROBE_ROWS",
    "DUMP_PROBE_ENABLE_FILE",
]


class ProbeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        dump_probe._PROBE.__init__()  # fresh state per test
        os.environ["DUMP_PROBE"] = "1"

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class EnableTests(ProbeTestCase):
    def test_disabled_without_master_switch(self) -> None:
        os.environ.pop("DUMP_PROBE")
        self.assertFalse(dump_probe.enabled())
        self.assertFalse(dump_probe.arm("req-1", rank=0))

    def test_non_one_value_is_disabled(self) -> None:
        os.environ["DUMP_PROBE"] = "true"
        self.assertFalse(dump_probe.enabled())

    def test_sentinel_file_gates_arming(self) -> None:
        missing = "/tmp/ascend-tensor-dump-sentinel-does-not-exist"
        os.environ["DUMP_PROBE_ENABLE_FILE"] = missing
        self.assertFalse(dump_probe.enabled())

    def test_capture_is_noop_when_not_armed(self) -> None:
        dump_probe.capture("stage", FakeTensor())
        self.assertEqual(dump_probe._PROBE.records, [])


class SelectorTests(ProbeTestCase):
    def test_only_configured_ranks_arm(self) -> None:
        os.environ["DUMP_PROBE_RANKS"] = "3"
        self.assertFalse(dump_probe.arm("req-1", rank=0))
        self.assertTrue(dump_probe.arm("req-1", rank=3))

    def test_multiple_ranks_are_parsed(self) -> None:
        os.environ["DUMP_PROBE_RANKS"] = "0, 5 ,"
        self.assertFalse(dump_probe.arm("req-1", rank=1))
        self.assertTrue(dump_probe.arm("req-1", rank=5))

    def test_match_counts_distinct_labels_not_calls(self) -> None:
        """Three prefill chunks of one request must not look like three requests."""
        os.environ["DUMP_PROBE_MATCH"] = "2"

        # request A arrives as three chunks: all are label #1, none should arm
        for _ in range(3):
            self.assertFalse(dump_probe.arm("req-A", rank=0))
        # request B is the second distinct label and arms on its first chunk
        self.assertTrue(dump_probe.arm("req-B", rank=0))
        self.assertEqual(dump_probe._PROBE.label, "req-B")
        self.assertEqual(dump_probe._PROBE.match_index, 2)
        self.assertEqual(dump_probe._PROBE.occurrence, 1)

    def test_occurrence_selects_a_later_chunk_of_the_same_request(self) -> None:
        os.environ["DUMP_PROBE_OCCURRENCE"] = "3"
        self.assertFalse(dump_probe.arm("req-A", rank=0))
        self.assertFalse(dump_probe.arm("req-A", rank=0))
        self.assertTrue(dump_probe.arm("req-A", rank=0))
        self.assertEqual(dump_probe._PROBE.occurrence, 3)

    def test_cannot_rearm_while_armed(self) -> None:
        self.assertTrue(dump_probe.arm("req-A", rank=0))
        self.assertFalse(dump_probe.arm("req-A", rank=0))

    def test_label_is_not_rearmed_after_it_was_dumped(self) -> None:
        self.assertTrue(dump_probe.arm("req-A", rank=0))
        # simulate finish() completing for this label
        dump_probe._PROBE.dumped_labels.add("req-A")
        dump_probe._PROBE.reset_capture()
        self.assertFalse(dump_probe.arm("req-A", rank=0))

    def test_invalid_integer_env_falls_back_to_default(self) -> None:
        os.environ["DUMP_PROBE_MATCH"] = "first"
        self.assertTrue(dump_probe.arm("req-A", rank=0))

    def test_metadata_is_copied_onto_the_armed_forward(self) -> None:
        dump_probe.arm("req-A", rank=0, num_computed_tokens=7680)
        self.assertEqual(
            dump_probe._PROBE.metadata, {"num_computed_tokens": 7680}
        )


class CaptureTests(ProbeTestCase):
    def setUp(self) -> None:
        super().setUp()
        dump_probe.arm("req-A", rank=0)

    def test_layout_is_recorded_even_without_statistics(self) -> None:
        dump_probe.capture("layers.0.attn", FakeTensor(4, 8))
        record = dump_probe._PROBE.records[0]
        self.assertEqual(record["stage"], "layers.0.attn")
        self.assertEqual(record["order"], 0)
        self.assertEqual(record["shape"], [4, 8])
        self.assertEqual(record["stride"], [8, 1])
        self.assertEqual(record["numel"], 32)
        self.assertTrue(record["contiguous"])

    def test_no_tensor_is_cloned_by_default(self) -> None:
        dump_probe.capture("layers.0.attn", FakeTensor())
        self.assertEqual(dump_probe._PROBE.tensors, [])
        self.assertNotIn("tensor_saved", dump_probe._PROBE.records[0])

    def test_tensor_pattern_gates_cloning(self) -> None:
        os.environ["DUMP_PROBE_TENSOR"] = r"layers\.23\."
        dump_probe.capture("layers.0.attn", FakeTensor())
        dump_probe.capture("layers.23.attn", FakeTensor())
        self.assertEqual(
            [stage for stage, _ in dump_probe._PROBE.tensors], ["layers.23.attn"]
        )

    def test_rows_limit_slices_dim_zero(self) -> None:
        os.environ["DUMP_PROBE_TENSOR"] = "."
        os.environ["DUMP_PROBE_ROWS"] = "2"
        dump_probe.capture("stage", FakeTensor(64, 8))
        _, saved = dump_probe._PROBE.tensors[0]
        self.assertEqual(saved.shape, (2, 8))

    def test_rows_limit_does_not_pad_small_tensors(self) -> None:
        os.environ["DUMP_PROBE_TENSOR"] = "."
        os.environ["DUMP_PROBE_ROWS"] = "64"
        dump_probe.capture("stage", FakeTensor(4, 8))
        _, saved = dump_probe._PROBE.tensors[0]
        self.assertEqual(saved.shape, (4, 8))

    def test_rows_limit_leaves_one_dimensional_tensors_whole(self) -> None:
        # A 1-D tensor is a weight or per-channel parameter. Slicing its only
        # dimension corrupts it instead of shrinking the dump.
        os.environ["DUMP_PROBE_TENSOR"] = "."
        os.environ["DUMP_PROBE_ROWS"] = "8"
        dump_probe.capture("gamma", FakeTensor(1024))
        _, saved = dump_probe._PROBE.tensors[0]
        self.assertEqual(saved.shape, (1024,))

    def test_summary_pattern_limits_which_stages_are_measured(self) -> None:
        os.environ["DUMP_PROBE_SUMMARY"] = "kv$"
        dump_probe.capture("layers.0.attn", FakeTensor())
        dump_probe.capture("layers.0.kv", FakeTensor())
        # torch is absent here, so a measured stage reports the failure instead
        # of silently looking like an unmeasured one
        self.assertNotIn("summary_error", dump_probe._PROBE.records[0])
        self.assertIn("summary_error", dump_probe._PROBE.records[1])

    def test_empty_tensor_is_not_measured(self) -> None:
        dump_probe.capture("stage", FakeTensor(0, 8))
        record = dump_probe._PROBE.records[0]
        self.assertEqual(record["numel"], 0)
        self.assertNotIn("summary_error", record)

    def test_non_tensor_argument_is_ignored(self) -> None:
        dump_probe.capture("stage", 5)
        dump_probe.capture("stage", None)
        self.assertEqual(dump_probe._PROBE.records, [])

    def test_capture_inputs_keeps_non_tensor_arguments_verbatim(self) -> None:
        dump_probe.capture_inputs(
            "gmm1", hidden=FakeTensor(4, 8), group_list_type=1, trans_b=True
        )
        stage, payload = dump_probe._PROBE.input_sets[0]
        self.assertEqual(stage, "gmm1")
        self.assertEqual(payload["group_list_type"], 1)
        self.assertIs(payload["trans_b"], True)
        record = dump_probe._PROBE.records[0]
        self.assertEqual(record["inputs"]["hidden"]["shape"], [4, 8])
        self.assertEqual(record["inputs"]["trans_b"], {"value": "True"})

    def test_capture_inputs_respects_row_limit(self) -> None:
        os.environ["DUMP_PROBE_ROWS"] = "3"
        dump_probe.capture_inputs("gmm1", hidden=FakeTensor(64, 8))
        _, payload = dump_probe._PROBE.input_sets[0]
        self.assertEqual(payload["hidden"].shape, (3, 8))
        # the manifest keeps the true shape, not the truncated one
        record = dump_probe._PROBE.records[0]
        self.assertEqual(record["inputs"]["hidden"]["shape"], [64, 8])

    def test_capture_inputs_keeps_weights_replayable(self) -> None:
        # Row-limiting a captured weight makes the input set unreplayable:
        # the operator rejects gamma[:8] against a [1, 1024] activation.
        os.environ["DUMP_PROBE_ROWS"] = "8"
        dump_probe.capture_inputs(
            "add_rms_norm", x=FakeTensor(1, 1024), gamma=FakeTensor(1024)
        )
        _, payload = dump_probe._PROBE.input_sets[0]
        self.assertEqual(payload["x"].shape, (1, 1024))
        self.assertEqual(payload["gamma"].shape, (1024,))


class GraphTests(ProbeTestCase):
    def test_capture_graph_without_a_slot_is_a_noop(self) -> None:
        dump_probe.capture_graph("missing", FakeTensor())
        self.assertEqual(dump_probe._PROBE.graph_slots, {})


class StubNpu:
    def __init__(self) -> None:
        self.synchronize_calls = 0
        self.capturing = False

    def synchronize(self) -> None:
        self.synchronize_calls += 1

    def is_current_stream_capturing(self) -> bool:
        return self.capturing


class StubStack:
    def __init__(self, rows: list[list[float]]) -> None:
        self._rows = rows

    def cpu(self) -> "StubStack":
        return self

    def tolist(self) -> list[list[float]]:
        return self._rows


class StubTorch:
    """Just enough torch for finish() to be exercised without the real one."""

    def __init__(self) -> None:
        self.npu = StubNpu()
        self.saved: list[tuple[dict, str]] = []

    def stack(self, values):
        return StubStack(list(values))

    def save(self, payload, path) -> None:
        self.saved.append((payload, str(path)))
        Path(path).write_bytes(b"stub")


class FinishTests(ProbeTestCase):
    """finish() is the only place that synchronizes and writes, so it gets its
    own coverage: one read-back, correct statistic mapping, stable filenames."""

    def setUp(self) -> None:
        super().setUp()
        import sys

        self.torch = StubTorch()
        self._had_torch = "torch" in sys.modules
        self._prior_torch = sys.modules.get("torch")
        sys.modules["torch"] = self.torch  # type: ignore[assignment]
        self.out = tempfile.mkdtemp()
        os.environ["DUMP_PROBE_DIR"] = self.out

    def tearDown(self) -> None:
        import shutil
        import sys

        if self._had_torch:
            sys.modules["torch"] = self._prior_torch  # type: ignore[assignment]
        else:
            sys.modules.pop("torch", None)
        shutil.rmtree(self.out, ignore_errors=True)
        super().tearDown()

    def arm_with_stats(self, rows: list[list[float]]) -> None:
        """Arm and attach pre-computed device statistics.

        The statistics themselves are produced by torch on the NPU; what is
        worth testing here is that finish() maps one stacked read-back back
        onto the right records.
        """
        os.environ["DUMP_PROBE_SUMMARY"] = "$never$"
        dump_probe.arm("req-A", rank=0, note="unit")
        for index, _ in enumerate(rows):
            dump_probe.capture(f"stage-{index}", FakeTensor())
            dump_probe._PROBE.stat_tensors.append(rows[index])
            dump_probe._PROBE.records[index]["summary_index"] = index

    def test_returns_none_when_not_armed(self) -> None:
        self.assertIsNone(dump_probe.finish())

    def test_refuses_to_read_back_during_graph_capture(self) -> None:
        dump_probe.arm("req-A", rank=0)
        self.torch.npu.capturing = True
        self.assertIsNone(dump_probe.finish())
        self.assertEqual(self.torch.npu.synchronize_calls, 0)

    def test_writes_manifest_with_mapped_statistics(self) -> None:
        self.arm_with_stats(
            [
                [0.0, 0.0, 1.5, -1.0, 1.5, 0.1],
                [448.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            ]
        )
        path = dump_probe.finish()
        self.assertIsNotNone(path)
        payload = json.loads(Path(path).read_text(encoding="utf-8"))

        self.assertEqual(payload["label"], "req-A")
        self.assertEqual(payload["rank"], 0)
        self.assertEqual(payload["metadata"], {"note": "unit"})
        self.assertEqual(payload["stats"], list(dump_probe.STAT_NAMES))
        self.assertEqual(payload["records"][0]["summary"]["max_abs"], 1.5)
        self.assertEqual(payload["records"][1]["summary"]["nan_count"], 448.0)
        # the transient index must not leak into the manifest
        self.assertNotIn("summary_index", payload["records"][0])

    def test_synchronizes_exactly_once(self) -> None:
        self.arm_with_stats([[0.0] * 6])
        dump_probe.finish()
        self.assertEqual(self.torch.npu.synchronize_calls, 1)

    def test_no_tensor_file_when_nothing_was_cloned(self) -> None:
        self.arm_with_stats([[0.0] * 6])
        path = dump_probe.finish()
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertNotIn("tensor_file", payload)
        self.assertEqual(self.torch.saved, [])

    def test_tensor_file_is_written_when_a_stage_was_cloned(self) -> None:
        os.environ["DUMP_PROBE_TENSOR"] = "."
        dump_probe.arm("req-A", rank=0)
        dump_probe.capture("layers.23.kv", FakeTensor())
        path = dump_probe.finish()
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(payload["tensor_file"], "req-A-rank0.pt")
        self.assertTrue((Path(self.out) / "req-A-rank0.pt").exists())

    def test_state_is_reset_and_the_label_is_not_dumped_twice(self) -> None:
        self.arm_with_stats([[0.0] * 6])
        dump_probe.finish()
        self.assertFalse(dump_probe.armed())
        self.assertEqual(dump_probe._PROBE.records, [])
        self.assertEqual(dump_probe._PROBE.stat_tensors, [])
        self.assertFalse(dump_probe.arm("req-A", rank=0))

    def test_filename_is_derived_from_label_and_rank(self) -> None:
        os.environ["DUMP_PROBE_RANKS"] = "2"
        dump_probe.arm("cmpl-a/b", rank=2)
        path = dump_probe.finish()
        self.assertEqual(Path(path).name, "cmpl-a_b-rank2.json")


class NamingTests(ProbeTestCase):
    def test_label_is_sanitized_for_the_filename(self) -> None:
        self.assertEqual(
            dump_probe._safe_stem("cmpl-a/b c:d", 3), "cmpl-a_b_c_d-rank3"
        )

    def test_stat_names_are_stable(self) -> None:
        self.assertEqual(
            dump_probe.STAT_NAMES,
            ("nan_count", "inf_count", "max_abs", "min", "max", "mean"),
        )


if __name__ == "__main__":
    unittest.main()
