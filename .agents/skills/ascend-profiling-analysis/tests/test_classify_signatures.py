"""Regression tests for the classify stage's single-pass signature build.

``classify.classify_profile`` used to compute block, layer, and step class
signatures in three independent walks over the rank events -- each walk
re-sliced, re-sorted, and re-normalized every event.  It now builds one
per-rank pair array in a single streaming pass (``_rank_pair_index``) and
derives every member's signature as a contiguous subsequence of it
(``_pairs_in_range``), with ``_normalized_name_key`` memoized.  These
tests pin the invariants that make the refactor semantics-preserving:

1. ``_normalized_name_key`` stays a pure mapping (identical to the
   ``segment.normalized_name_key`` convention it mirrors) and repeat calls
   are served from the memo cache -- misses scale with distinct names,
   not with event count.
2. ``_pairs_in_range`` over any row range reproduces ``_shape_pairs``
   over the sliced events exactly, in content and order -- including the
   step head/tail rows that belong to no layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ascend_profile import classify, segment  # noqa: E402
from ascend_profile.common import NormalizedEvent  # noqa: E402


def _event(row_idx: int, name: str, sig: str | None, start_us: float | None = None) -> NormalizedEvent:
    start = float(row_idx * 10 if start_us is None else start_us)
    return NormalizedEvent(
        event_id=f"evt_0_{row_idx}",
        profile_id="prof",
        rank_id="rank0",
        source_id="src0",
        row_idx=row_idx,
        name_raw=name,
        task_type="KERNEL",
        accelerator_core="AI_CORE",
        stream_id="7",
        start_us=start,
        end_us=start + 1.0,
        duration_us=1.0,
        wait_us=0.0,
        shape_signature=sig,
    )


def test_normalized_name_key_is_memoized() -> None:
    classify._normalized_name_key.cache_clear()
    name_a = "MatMulV3_0x1a2b3c_42"
    name_b = "RmsNorm_12_34"
    first = classify._normalized_name_key(name_a)
    second = classify._normalized_name_key(name_a)
    classify._normalized_name_key(name_b)
    assert first == second
    info = classify._normalized_name_key.cache_info()
    # Two distinct names -> two misses; the repeat call -> one hit.
    assert (info.misses, info.hits) == (2, 1)
    classify._normalized_name_key.cache_clear()


def test_normalized_name_key_matches_segment_convention() -> None:
    names = [
        "MatMulV3",
        "aten::add_0x1a2b3c",
        "RmsNorm_12_34",
        "MoeDistributeDispatch_0123456789abcdef_7",
        "hcom_allreduce__42",
        "",
        "___",
        "0xdeadbeef",
    ]
    for name in names:
        assert classify._normalized_name_key(name) == segment.normalized_name_key(name)


def test_pairs_in_range_matches_shape_pairs_oracle() -> None:
    events = [
        _event(0, "MatMulV3_1", "1x2"),
        _event(1, "RmsNorm_9", None),
        _event(2, "MatMulV3_1", "3x4"),
        _event(3, "AddRmsNorm_2", ""),
        _event(4, "SwiGLU_5", "5x6"),
        _event(5, "MatMulV3_1", "1x2"),
    ]
    row_indexes, pair_at = classify._rank_pair_index(events)
    assert row_indexes == [0, 1, 2, 3, 4, 5]
    # Shape-bearing events produce pairs; None / "" signatures produce holes.
    assert [pair is not None for pair in pair_at] == [True, False, True, False, True, True]
    for row_start, row_end in ((0, 5), (0, 0), (2, 4), (1, 3), (4, 5), (3, 3), (0, 100)):
        expected = classify._shape_pairs(classify._event_slice(events, row_indexes, row_start, row_end))
        assert classify._pairs_in_range(row_indexes, pair_at, row_start, row_end) == expected


def test_pairs_in_range_edge_cases() -> None:
    events = [_event(0, "MatMulV3_1", "1x2"), _event(1, "RmsNorm_9", None)]
    row_indexes, pair_at = classify._rank_pair_index(events)
    # Inverted range and fully-outside ranges are empty.
    assert classify._pairs_in_range(row_indexes, pair_at, 5, 4) == ()
    assert classify._pairs_in_range(row_indexes, pair_at, 10, 20) == ()
    # A range with no shape-bearing events is empty too.
    assert classify._pairs_in_range(row_indexes, pair_at, 1, 1) == ()
    # Bounds are inclusive on both ends.
    assert classify._pairs_in_range(row_indexes, pair_at, 0, 0) == classify._shape_pairs(events[:1])


def test_step_range_pairs_include_head_tail_rows_outside_layers() -> None:
    # Layout: two "layers" (rows 1-2 and 4-5) with head row 0, gap row 3,
    # and tail row 6 that belong to no layer.  The step range 0-6 must
    # fingerprint those rows; the layer ranges must not.
    events = [
        _event(0, "GraphEntry_1", "hxw"),
        _event(1, "MatMulV3_1", "1x2"),
        _event(2, "RmsNorm_9", None),
        _event(3, "hccl_allreduce_3", "mbx1"),
        _event(4, "MatMulV3_2", "3x4"),
        _event(5, "SwiGLU_5", "5x6"),
        _event(6, "ArgMax_7", "vocab"),
    ]
    row_indexes, pair_at = classify._rank_pair_index(events)
    step_pairs = classify._pairs_in_range(row_indexes, pair_at, 0, 6)
    layer_a = classify._pairs_in_range(row_indexes, pair_at, 1, 2)
    layer_b = classify._pairs_in_range(row_indexes, pair_at, 4, 5)
    assert step_pairs == classify._shape_pairs(events)
    # Head / gap / tail pairs are present in the step fingerprint but in
    # neither layer fingerprint.
    head_tail = {
        (classify._normalized_name_key("GraphEntry_1"), "hxw"),
        (classify._normalized_name_key("hccl_allreduce_3"), "mbx1"),
        (classify._normalized_name_key("ArgMax_7"), "vocab"),
    }
    assert head_tail <= set(step_pairs)
    assert not (head_tail & (set(layer_a) | set(layer_b)))
    # A block row range is a subsequence of its layer's pairs, in order.
    block_pairs = classify._pairs_in_range(row_indexes, pair_at, 4, 4)
    assert block_pairs == layer_b[:1]


def test_rank_pair_index_misses_scale_with_distinct_names() -> None:
    classify._normalized_name_key.cache_clear()
    events = [
        _event(0, "MatMulV3_1", "1x2"),
        _event(1, "MatMulV3_1", "3x4"),
        _event(2, "RmsNorm_9", None),
        _event(3, "MatMulV3_2", "5x6"),
    ]
    classify._rank_pair_index(events)
    info = classify._normalized_name_key.cache_info()
    # Three shape-bearing events but only two distinct raw names.
    assert info.misses == 2
    assert info.hits == 1
    classify._normalized_name_key.cache_clear()
