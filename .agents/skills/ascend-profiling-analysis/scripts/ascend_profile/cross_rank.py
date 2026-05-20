#!/usr/bin/env python3
"""Create cross-rank alignment evidence tables."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

try:
    from .common import (
        CrossRankAlignment,
        NormalizedEvent,
        SCHEMA_VERSION,
        TOOL_VERSION,
        emit_stage_json,
        group_by_rank,
        load_events,
        load_step_segments,
        metrics_for_events,
        stable_id,
        utc_now,
        write_csv,
        write_json,
    )
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import (
        # type: ignore[no-redef]
        CrossRankAlignment,
        NormalizedEvent,
        SCHEMA_VERSION,
        TOOL_VERSION,
        emit_stage_json,
        group_by_rank,
        load_events,
        load_step_segments,
        metrics_for_events,
        stable_id,
        utc_now,
        write_csv,
        write_json,
    )


STEP_TIME_OVERLAP_RATIO = 0.20
OPERATOR_ALIGNMENT_BUCKET_US = 1000.0

# Alignment method tags + their default confidence/limitation envelopes.
# Embedded in every alignment row so downstream diagnostics and the
# evidence-chain validator can reason about how trustworthy each
# alignment is.
STEP_ALIGNMENT_METHOD = "step_time_overlap_v1"
OPERATOR_ALIGNMENT_METHOD = "time_bucket_v1"
STEP_ALIGNMENT_LIMITATIONS = (
    "Step alignment uses raw start/end overlap; ranks captured with "
    "different start offsets or non-overlapping time windows will be "
    "treated as independent."
)
OPERATOR_ALIGNMENT_LIMITATIONS = (
    "Operator alignment buckets events by (role, name_key, shape, "
    "floor(start_us / bucket_us)). Multiple ops landing in the same "
    "bucket are assumed to be the same logical op across ranks; "
    "non-overlapping schedules or operator-level reordering can mis-pair."
)


def overlap_us(left_start: float, left_end: float, right_start: float, right_end: float) -> float:
    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def alignment_row(alignment: CrossRankAlignment) -> dict[str, Any]:
    return {
        "alignment_id": alignment.alignment_id,
        "alignment_type": alignment.alignment_type,
        "rank_ids": list(alignment.rank_ids),
        "segment_ids": list(alignment.segment_ids),
        "event_ids": list(alignment.event_ids),
        "start_us": alignment.start_us,
        "end_us": alignment.end_us,
        # ``alignment_method`` / ``alignment_confidence`` / ``limitations``
        # are surfaced as first-class columns rather than nested metrics so
        # the CSV stays consumable by spreadsheets and the evidence-chain
        # validator can read them without parsing JSON.
        "alignment_method": alignment.metrics.get("alignment_method"),
        "alignment_confidence": alignment.metrics.get("alignment_confidence"),
        "alignment_limitations": alignment.metrics.get("alignment_limitations"),
        **alignment.metrics,
        "evidence_ids": list(alignment.evidence_ids),
    }


def _step_confidence(members_count: int, wall_skew_us: float, layer_mismatch: bool) -> str:
    if layer_mismatch:
        return "low"
    if members_count >= 4 and wall_skew_us <= 5000.0:
        return "high"
    if members_count >= 2 and wall_skew_us <= 20000.0:
        return "medium"
    return "low"


def _operator_confidence(member_count: int, start_skew_us: float, duration_ratio: float) -> str:
    if duration_ratio >= 5.0 or start_skew_us >= 20000.0:
        return "low"
    if member_count >= 4 and duration_ratio <= 1.5 and start_skew_us <= 2000.0:
        return "high"
    if member_count >= 2 and duration_ratio <= 2.5 and start_skew_us <= 10000.0:
        return "medium"
    return "low"


def event_alignment_key(event: NormalizedEvent) -> tuple[str, str, str]:
    if "communication.collective" in event.op_categories:
        role = "communication.collective"
    elif "moe.dispatch_expert_compute" in event.op_categories:
        role = "moe.dispatch_expert_compute"
    elif "moe.dispatch" in event.op_categories or "moe.combine" in event.op_categories:
        role = "moe.dispatch_or_combine"
    elif "compute.matmul" in event.op_categories:
        role = "compute.matmul"
    elif any(category.startswith("attention.") for category in event.op_categories):
        role = "attention"
    else:
        role = "other"
    shape = event.shape_signature or "no_shape"
    name_key = event.name_raw if role in {"communication.collective", "moe.dispatch_expert_compute"} else role
    return role, name_key, shape


def build_step_alignments(segments: Sequence[Any]) -> list[CrossRankAlignment]:
    steps = [segment for segment in segments if segment.segment_type == "step"]
    alignments: list[CrossRankAlignment] = []
    seen: set[tuple[str, ...]] = set()
    for step in steps:
        members = [step]
        for other in steps:
            if other.rank_id == step.rank_id:
                continue
            overlap = overlap_us(step.start_us, step.end_us, other.start_us, other.end_us)
            if overlap <= 0:
                continue
            denom = max(1.0, min(step.end_us - step.start_us, other.end_us - other.start_us))
            if overlap / denom >= STEP_TIME_OVERLAP_RATIO:
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
                alignment_id=stable_id("align", "step", segment_ids),
                alignment_type="time_window",
                rank_ids=rank_ids,
                segment_ids=segment_ids,
                start_us=start,
                end_us=end,
                metrics={
                    "alignment_method": STEP_ALIGNMENT_METHOD,
                    "alignment_confidence": _step_confidence(
                        len(members), wall_skew_us, layer_mismatch
                    ),
                    "alignment_limitations": STEP_ALIGNMENT_LIMITATIONS,
                    "member_count": len(members),
                    "wall_skew_us": wall_skew_us,
                    "layer_counts": layer_counts,
                    "step_families": families,
                    "is_structure_mismatch": layer_mismatch,
                },
            )
        )
    return alignments


def build_operator_alignments(events: Sequence[NormalizedEvent], *, bucket_us: float = OPERATOR_ALIGNMENT_BUCKET_US) -> list[CrossRankAlignment]:
    grouped: dict[tuple[str, str, str, int], list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        role, name_key, shape = event_alignment_key(event)
        if role == "other":
            continue
        bucket = int(event.start_us // bucket_us)
        grouped[(role, name_key, shape, bucket)].append(event)
    alignments: list[CrossRankAlignment] = []
    for (role, name_key, shape, bucket), items in grouped.items():
        rank_ids = tuple(sorted({event.rank_id for event in items}))
        if len(rank_ids) < 2:
            continue
        starts = [event.start_us for event in items]
        durations = [event.duration_us for event in items]
        waits = [event.wait_us for event in items]
        start_skew_us = round(max(starts) - min(starts), 3)
        duration_ratio = round(max(durations) / max(1e-6, min(durations)), 6)
        alignments.append(
            CrossRankAlignment(
                alignment_id=stable_id("align", role, name_key, shape, bucket),
                alignment_type="operator",
                rank_ids=rank_ids,
                event_ids=tuple(event.event_id for event in sorted(items, key=lambda item: (item.rank_id, item.start_us))),
                start_us=min(event.start_us for event in items),
                end_us=max(event.end_us for event in items),
                metrics={
                    "alignment_method": OPERATOR_ALIGNMENT_METHOD,
                    "alignment_confidence": _operator_confidence(
                        len(items), start_skew_us, duration_ratio
                    ),
                    "alignment_limitations": OPERATOR_ALIGNMENT_LIMITATIONS,
                    "role": role,
                    "name_key": name_key,
                    "shape_signature": shape,
                    "bucket_us": bucket_us,
                    "member_count": len(items),
                    "rank_count": len(rank_ids),
                    "start_skew_us": start_skew_us,
                    "duration_min_us": round(min(durations), 3),
                    "duration_max_us": round(max(durations), 3),
                    "duration_skew_us": round(max(durations) - min(durations), 3),
                    "duration_ratio": duration_ratio,
                    "wait_max_us": round(max(waits), 3),
                },
            )
        )
    alignments.sort(key=lambda item: (str(item.metrics.get("role")), float(item.start_us or 0.0)))
    return alignments


def cross_rank_profile(output_dir: Path) -> dict[str, Any]:
    events = load_events(output_dir / "normalized_event_index.jsonl")
    segments = load_step_segments(output_dir / "step_segments.json")
    step_alignments = build_step_alignments(segments)
    operator_alignments = build_operator_alignments(events)
    alignments = step_alignments + operator_alignments
    rows = [alignment_row(alignment) for alignment in alignments]
    write_csv(output_dir / "cross_rank_alignment.csv", rows)
    write_json(output_dir / "cross_rank_alignment.json", {"cross_rank_alignments": alignments})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "cross_rank",
        "created_at": utc_now(),
        "output_dir": str(output_dir),
        "files": {
            "cross_rank_alignment": "cross_rank_alignment.csv",
            "cross_rank_alignment_json": "cross_rank_alignment.json",
        },
        "counts": {
            "alignment_count": len(alignments),
            "step_alignment_count": len(step_alignments),
            "operator_alignment_count": len(operator_alignments),
            "rank_count": len(group_by_rank(events)),
        },
    }
    write_json(output_dir / "cross_rank_manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = cross_rank_profile(Path(args.output))
    emit_stage_json({"stage": "cross_rank", "counts": manifest["counts"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
