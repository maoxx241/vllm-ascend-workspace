"""Segment split performance guards: KMP minimal period parity + budget breaker.

``minimal_exact_period`` used to be an O(n^2) divisor scan and
``split_composite_frames`` had no circuit breaker; on a 25k-layer merged
frame (Kimi-K3 period-4 hybrid) the segment stage ground for >20 minutes
before being stack-sampled. These tests pin the KMP equivalence and the
budget-breaker behaviour.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ascend_profile.segment import (  # noqa: E402
    LayerFrame,
    LayerObservation,
    _split_frames_with_budget,
    _tag_frames,
    minimal_exact_period,
)


def _naive_minimal_exact_period(sequence):
    if not sequence:
        return ()
    values = tuple(sequence)
    for width in range(1, len(values) + 1):
        if len(values) % width != 0:
            continue
        unit = values[:width]
        if unit * (len(values) // width) == values:
            return unit
    return values


@pytest.mark.parametrize("seed", range(40))
def test_kmp_matches_naive_minimal_period(seed: int) -> None:
    rng = random.Random(seed)
    alphabet = ["a", "b", "c", "d"]
    length = rng.randint(0, 60)
    sequence = tuple(rng.choice(alphabet) for _ in range(length))
    assert minimal_exact_period(sequence) == _naive_minimal_exact_period(sequence)


@pytest.mark.parametrize(
    "sequence,expected",
    [
        ((), ()),
        (("x",), ("x",)),
        (("a", "a", "a", "a"), ("a",)),
        (("a", "b", "a", "b"), ("a", "b")),
        (("a", "b", "b", "a"), ("a", "b", "b", "a")),
        (("a", "b", "c", "a", "b", "c", "a", "b", "c"), ("a", "b", "c")),
        (("k", "k", "k", "m", "k", "k", "k", "m"), ("k", "k", "k", "m")),
    ],
)
def test_kmp_reference_cases(sequence, expected) -> None:
    assert minimal_exact_period(sequence) == expected


def _frame(tag_suffix: str, rows: tuple[int, int] = (0, 10)) -> LayerFrame:
    layer = LayerObservation(
        index=0,
        row_start=rows[0],
        row_end=rows[1],
        anchors=(),
        signature="sig",
        regime_key="sig",
    )
    return LayerFrame(layers=(layer,), reason=f"test-{tag_suffix}")


def test_tag_frames_appends_tag_and_preserves_rows() -> None:
    frame = _frame("a")
    tagged = _tag_frames([frame], "composite_split_budget_exceeded")
    assert tagged[0].tags == ("composite_split_budget_exceeded",)
    assert tagged[0].row_start == frame.row_start
    assert tagged[0].row_end == frame.row_end


def test_split_with_budget_exhausted_returns_tagged_unsplit() -> None:
    frames = [_frame("a", (0, 10)), _frame("b", (10, 20))]
    out = _split_frames_with_budget(frames, time.monotonic() - 1.0, pass_name="test")
    assert len(out) == 2
    assert all("composite_split_budget_exceeded" in frame.tags for frame in out)
    # Row coverage preserved (lossless), just unsplit.
    assert out[0].row_start == 0 and out[-1].row_end == 20
