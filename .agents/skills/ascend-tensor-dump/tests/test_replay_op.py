"""Tests for the stage addressing helpers in replay_op.py.

The replay itself needs an NPU, so only the pure input-set bookkeeping is
covered here. That bookkeeping is what decides whether you replay the layer
you meant to replay.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ASSET = Path(__file__).resolve().parents[1] / "assets" / "replay_op.py"
SPEC = importlib.util.spec_from_file_location("replay_op_under_test", ASSET)
assert SPEC and SPEC.loader
replay_op = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay_op
SPEC.loader.exec_module(replay_op)


class ResolveStageTests(unittest.TestCase):
    def test_inplace_candidate_cannot_change_reference_inputs(self):
        class Tensor:
            def __init__(self, value): self.value = value
            def to(self, device): return self
            def clone(self, *, memory_format):
                self.asserted_format = memory_format
                return Tensor(self.value)
        tensor = Tensor(5)
        observed = []
        def candidate(x, nested):
            self.assertIs(x, nested[0])
            observed.append(x.value)
            x.value = 99
            return x
        def reference(x, nested):
            self.assertIs(x, nested[0])
            observed.append(x.value)
            return x
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"torch": SimpleNamespace(Tensor=Tensor, preserve_format="preserve")}), patch.object(
            replay_op, "load_input_sets", return_value={"op#0": {"x": tensor, "nested": [tensor]}}
        ), patch.object(replay_op, "resolve", side_effect=[candidate, reference]), patch.object(replay_op, "save_payload"), redirect_stdout(io.StringIO()):
            self.assertEqual(replay_op.main(["--dump", "dump.pt", "--stage", "op", "--candidate", "candidate", "--reference", "reference", "--device", "cpu", "--out-dir", tmp]), 0)
        self.assertEqual(observed, [5, 5])
        self.assertEqual(tensor.value, 5)
        self.assertEqual(tensor.asserted_format, "preserve")

    def test_exact_key_wins(self) -> None:
        sets = {"gmm#0": {}, "gmm#1": {}}
        self.assertEqual(replay_op.resolve_stage("gmm#1", sets), "gmm#1")

    def test_bare_name_resolves_when_captured_once(self) -> None:
        sets = {"gmm#0": {}}
        self.assertEqual(replay_op.resolve_stage("gmm", sets), "gmm#0")

    def test_bare_name_is_rejected_when_ambiguous(self) -> None:
        sets = {"gmm#0": {}, "gmm#1": {}, "gmm#2": {}}
        with self.assertRaises(LookupError) as caught:
            replay_op.resolve_stage("gmm", sets)
        message = str(caught.exception)
        self.assertIn("captured 3 times", message)
        self.assertIn("gmm#0 .. gmm#2", message)

    def test_unknown_stage_is_reported(self) -> None:
        with self.assertRaises(LookupError) as caught:
            replay_op.resolve_stage("nope", {"gmm#0": {}})
        self.assertIn("not in dump", str(caught.exception))

    def test_a_prefix_is_not_treated_as_a_match(self) -> None:
        with self.assertRaises(LookupError):
            replay_op.resolve_stage("gm", {"gmm#0": {}})


class SummarizeStagesTests(unittest.TestCase):
    def test_repeats_are_counted_rather_than_collapsed(self) -> None:
        sets = {
            "rms#0": {"x": 1, "gamma": 2},
            "rms#1": {"x": 1, "gamma": 2},
            "attn#0": {"q": 1},
        }
        summary = replay_op.summarize_stages(sets)
        self.assertEqual(summary["rms"]["occurrences"], 2)
        self.assertEqual(summary["rms"]["inputs"], ["gamma", "x"])
        self.assertEqual(summary["rms"]["addressable_as"], "rms#0 .. rms#1")
        self.assertEqual(summary["attn"]["occurrences"], 1)
        self.assertEqual(summary["attn"]["addressable_as"], "attn#0")

    def test_empty_dump_summarizes_to_nothing(self) -> None:
        self.assertEqual(replay_op.summarize_stages({}), {})


if __name__ == "__main__":
    unittest.main()
