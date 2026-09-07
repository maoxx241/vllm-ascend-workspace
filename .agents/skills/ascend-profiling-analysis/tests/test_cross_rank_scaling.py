"""Regression tests for the cross_rank scaling work items:

- ``build_operator_alignments`` caps ``event_ids`` at 64 (the
  ``segment.add_evidence`` truncation precedent) while the full member
  count stays in ``metrics["member_count"]``;
- ``build_step_alignments`` rank-bucketed/pruned scan is exactly
  equivalent to the former all-pairs O(S^2) scan (the pre-optimization
  implementation is kept here verbatim as the equivalence oracle);
- ``cross_rank_profile`` counts distinct ranks without regrouping events.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import conftest  # noqa: F401 — registers sys.path

from ascend_profile import cross_rank
from ascend_profile.common import CrossRankAlignment, NormalizedEvent, StepSegment


def _event(event_id: str, rank_id: str, start_us: float, duration_us: float = 10.0) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        profile_id="profile",
        rank_id=rank_id,
        source_id="src",
        row_idx=0,
        name_raw="hcom_allreduce",
        task_type="",
        accelerator_core="",
        stream_id="",
        start_us=start_us,
        end_us=start_us + duration_us,
        duration_us=duration_us,
        wait_us=0.0,
        op_categories=("communication.collective",),
        shape_signature="shape_a",
    )


# ---------------------------------------------------------------------------
# event_ids truncation
# ---------------------------------------------------------------------------


def test_operator_alignment_event_ids_capped_at_64() -> None:
    events = [
        _event(f"evt_{index:03d}", rank_id=f"rank_{index % 8}", start_us=1000.0 + index)
        for index in range(200)
    ]
    alignments = cross_rank.build_operator_alignments(events)
    assert len(alignments) == 1
    alignment = alignments[0]
    assert len(alignment.event_ids) == 64
    assert alignment.metrics["member_count"] == 200
    assert alignment.metrics["rank_count"] == 8
    expected = [
        event.event_id
        for event in sorted(events, key=lambda item: (item.rank_id, item.start_us))[:64]
    ]
    assert list(alignment.event_ids) == expected


def test_operator_alignment_event_ids_not_padded_below_cap() -> None:
    events = [
        _event(f"evt_{index}", rank_id=f"rank_{index % 2}", start_us=1000.0 + index)
        for index in range(10)
    ]
    (alignment,) = cross_rank.build_operator_alignments(events)
    assert len(alignment.event_ids) == 10
    assert alignment.metrics["member_count"] == 10


# ---------------------------------------------------------------------------
# step alignment pruning equivalence
# ---------------------------------------------------------------------------


def _naive_build_step_alignments(segments):
    """Pre-optimization all-pairs implementation, kept verbatim as the
    equivalence oracle for the rank-bucketed scan."""
    steps = [segment for segment in segments if segment.segment_type == "step"]
    alignments = []
    seen = set()
    for step in steps:
        members = [step]
        for other in steps:
            if other.rank_id == step.rank_id:
                continue
            overlap = cross_rank.overlap_us(step.start_us, step.end_us, other.start_us, other.end_us)
            if overlap <= 0:
                continue
            denom = max(1.0, min(step.end_us - step.start_us, other.end_us - other.start_us))
            if overlap / denom >= cross_rank.STEP_TIME_OVERLAP_RATIO:
                members.append(other)
        rank_ids = tuple(sorted({member.rank_id for member in members}))
        segment_ids = tuple(sorted({member.segment_id for member in members}))
        if len(rank_ids) < 2 or segment_ids in seen:
            continue
        seen.add(segment_ids)
        start = min(member.start_us for member in members)
        end = max(member.end_us for member in members)
        layer_counts = [member.main_layer_count for member in members]
        families = sorted({member.step_family for member in members})
        wall_skew_us = round(
            max(member.end_us - member.start_us for member in members)
            - min(member.end_us - member.start_us for member in members),
            3,
        )
        layer_mismatch = (
            len(set((member.step_family, member.main_layer_count) for member in members)) > 1
        )
        alignments.append(
            CrossRankAlignment(
                alignment_id=cross_rank.stable_id("align", "step", segment_ids),
                alignment_type="time_window",
                rank_ids=rank_ids,
                segment_ids=segment_ids,
                start_us=start,
                end_us=end,
                metrics={
                    "alignment_method": cross_rank.STEP_ALIGNMENT_METHOD,
                    "alignment_confidence": cross_rank._step_confidence(
                        len(members), wall_skew_us, layer_mismatch
                    ),
                    "alignment_limitations": cross_rank.STEP_ALIGNMENT_LIMITATIONS,
                    "member_count": len(members),
                    "wall_skew_us": wall_skew_us,
                    "layer_counts": layer_counts,
                    "step_families": families,
                    "is_structure_mismatch": layer_mismatch,
                },
            )
        )
    return alignments


def _random_step_segments(seed: int, ranks: int = 4, steps_per_rank: int = 30) -> list[StepSegment]:
    rng = random.Random(seed)
    segments: list[StepSegment] = []
    for rank in range(ranks):
        cursor = rng.uniform(0.0, 500.0)
        for index in range(steps_per_rank):
            duration = rng.uniform(50.0, 400.0)
            segments.append(
                StepSegment(
                    segment_id=f"seg_{rank}_{index}",
                    rank_id=f"rank_{rank}",
                    segment_type="step",
                    complete=True,
                    row_start=0,
                    row_end=0,
                    start_us=cursor,
                    end_us=cursor + duration,
                    main_layer_count=rng.choice([3, 4]),
                    step_family=rng.choice(["train_step", "eval_step"]),
                )
            )
            cursor += duration + rng.uniform(0.0, 100.0)
    return segments


def test_step_alignment_pruned_scan_matches_naive_all_pairs() -> None:
    for seed in range(30):
        segments = _random_step_segments(seed)
        assert cross_rank.build_step_alignments(segments) == _naive_build_step_alignments(segments)


def test_step_alignment_abutting_steps_do_not_overlap() -> None:
    """``other.start_us == step.end_us`` is exactly the prune boundary and
    must stay a non-match (zero overlap)."""
    segments = [
        StepSegment(
            segment_id="seg_a",
            rank_id="rank_0",
            segment_type="step",
            complete=True,
            row_start=0,
            row_end=0,
            start_us=0.0,
            end_us=100.0,
            main_layer_count=3,
            step_family="train_step",
        ),
        StepSegment(
            segment_id="seg_b",
            rank_id="rank_1",
            segment_type="step",
            complete=True,
            row_start=0,
            row_end=0,
            start_us=100.0,
            end_us=200.0,
            main_layer_count=3,
            step_family="train_step",
        ),
    ]
    assert cross_rank.build_step_alignments(segments) == []
    assert _naive_build_step_alignments(segments) == []


def test_step_alignment_single_rank_never_aligns() -> None:
    segments = _random_step_segments(7, ranks=1, steps_per_rank=5)
    assert cross_rank.build_step_alignments(segments) == []


def test_step_alignment_non_step_segments_ignored() -> None:
    segments = _random_step_segments(3, ranks=2, steps_per_rank=4)
    islands = [
        StepSegment(
            segment_id="island",
            rank_id="rank_0",
            segment_type="unclassified_island",
            complete=False,
            row_start=0,
            row_end=0,
            start_us=0.0,
            end_us=100000.0,
        )
    ]
    assert cross_rank.build_step_alignments(islands + segments) == cross_rank.build_step_alignments(segments)


# ---------------------------------------------------------------------------
# rank_count without regrouping
# ---------------------------------------------------------------------------


def test_cross_rank_profile_counts_distinct_ranks(tmp_path: Path) -> None:
    events = [
        _event(f"evt_{index}", rank_id=f"rank_{index % 3}", start_us=1000.0 + index)
        for index in range(9)
    ]
    jsonl_path = tmp_path / "normalized_event_index.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for index, event in enumerate(events):
            handle.write(
                json.dumps(
                    {
                        "event_id": event.event_id,
                        "rank_id": event.rank_id,
                        "source_id": "src",
                        "row_idx": index,
                        "name_raw": event.name_raw,
                        "start_us": event.start_us,
                        "end_us": event.end_us,
                        "duration_us": event.duration_us,
                        # role "other": no operator categories, so this test
                        # exercises rank counting, not alignment building.
                        "op_categories": [],
                    }
                )
                + "\n"
            )
    manifest = cross_rank.cross_rank_profile(tmp_path)
    assert manifest["counts"]["rank_count"] == 3
    assert manifest["counts"]["alignment_count"] == 0
    assert json.loads((tmp_path / "cross_rank_alignment.json").read_text(encoding="utf-8")) == {
        "cross_rank_alignments": []
    }


if __name__ == "__main__":
    test_operator_alignment_event_ids_capped_at_64()
    print("ok")
