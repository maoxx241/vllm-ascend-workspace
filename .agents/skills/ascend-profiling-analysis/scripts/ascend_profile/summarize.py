#!/usr/bin/env python3
"""Create rank-local summaries from normalized events and segments."""

from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .common import (
        BlockSegment,
        EvidenceRef,
        Interval,
        NormalizedEvent,
        PIPELINE_FIELDS,
        SCHEMA_VERSION,
        TOOL_VERSION,
        bound_class_from_pipeline,
        bubble_windows,
        csv_value,
        emit_stage_json,
        group_by_rank,
        has_pipeline_signal,
        is_ai_core_like,
        is_aicpu_event,
        is_comm_event,
        load_block_segments,
        load_events,
        load_layer_segments,
        load_step_segments,
        metrics_for_events,
        quantile,
        read_json,
        row_ranges,
        stable_id,
        sum_pipeline_breakdown,
        utc_now,
        write_csv,
        write_json,
        write_jsonl,
    )
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import (
        # type: ignore[no-redef]
        BlockSegment,
        EvidenceRef,
        Interval,
        NormalizedEvent,
        PIPELINE_FIELDS,
        SCHEMA_VERSION,
        TOOL_VERSION,
        bound_class_from_pipeline,
        bubble_windows,
        csv_value,
        emit_stage_json,
        group_by_rank,
        has_pipeline_signal,
        is_ai_core_like,
        is_aicpu_event,
        is_comm_event,
        load_block_segments,
        load_events,
        load_layer_segments,
        load_step_segments,
        metrics_for_events,
        quantile,
        read_json,
        row_ranges,
        stable_id,
        sum_pipeline_breakdown,
        utc_now,
        write_csv,
        write_json,
        write_jsonl,
    )


UNDERFEED_HEAVY_RATIO = 0.30
INTERNAL_BUBBLE_MIN_MS = 1.0
INTERNAL_BUBBLE_WALL_RATIO = 0.10
WAIT_ANCHOR_RATIO = 0.80
FALSE_HOTSPOT_WAIT_RATIO = 0.95
FALSE_HOTSPOT_DURATION_US = 10.0
FALSE_HOTSPOT_TOP_RANK = 10
AICPU_MASKED_RATIO = 0.90
AICPU_PARTIAL_RATIO = 0.20


def anomaly_tags(metrics: Mapping[str, Any]) -> list[str]:
    tags: list[str] = []
    wall = float(metrics.get("wall_ms") or 0.0)
    if wall <= 0:
        return tags
    if float(metrics.get("underfeed_ratio") or 0.0) >= UNDERFEED_HEAVY_RATIO:
        tags.append("DEVICE_IDLE_GAP_HEAVY")
    if float(metrics.get("largest_internal_bubble_ms") or 0.0) >= max(INTERNAL_BUBBLE_MIN_MS, wall * INTERNAL_BUBBLE_WALL_RATIO):
        tags.append("INTERNAL_BUBBLE_HEAVY")
    return tags


def event_slice(events: Sequence[NormalizedEvent], row_numbers: Sequence[int], row_start: int, row_end: int) -> list[NormalizedEvent]:
    if row_end < row_start:
        return []
    left = bisect.bisect_left(row_numbers, int(row_start))
    right = bisect.bisect_right(row_numbers, int(row_end))
    return list(events[left:right])


def row_indexes_by_rank(events_by_rank: Mapping[str, Sequence[NormalizedEvent]]) -> dict[str, list[int]]:
    return {rank_id: [event.row_idx for event in events] for rank_id, events in events_by_rank.items()}


def _section_metrics(
    events: Sequence[NormalizedEvent],
    row_indexes: Sequence[int],
    row_start: int,
    row_end: int,
) -> dict[str, Any]:
    """Compute (wall, busy_union, bubble) for a row-range window inside a step.

    Returns a small dict keyed for the anatomy schema. Empty windows return
    zeros so downstream additions stay safe; ``event_count`` is the count of
    events whose row index lies in the requested closed range.
    """

    if row_end < row_start:
        return {
            "row_start": None,
            "row_end": None,
            "start_us": None,
            "end_us": None,
            "wall_ms": 0.0,
            "busy_ms": 0.0,
            "bubble_ms": 0.0,
            "event_count": 0,
        }
    sliced = event_slice(events, row_indexes, row_start, row_end)
    if not sliced:
        return {
            "row_start": int(row_start),
            "row_end": int(row_end),
            "start_us": None,
            "end_us": None,
            "wall_ms": 0.0,
            "busy_ms": 0.0,
            "bubble_ms": 0.0,
            "event_count": 0,
        }
    metrics = metrics_for_events(sliced, top_gap_limit=0)
    return {
        "row_start": int(row_start),
        "row_end": int(row_end),
        "start_us": metrics.get("start_us"),
        "end_us": metrics.get("end_us"),
        "wall_ms": float(metrics.get("wall_ms") or 0.0),
        "busy_ms": float(metrics.get("busy_union_ms") or 0.0),
        "bubble_ms": float(metrics.get("underfeed_ms") or 0.0),
        "event_count": int(metrics.get("event_count") or 0),
    }


def step_anatomy_rows(
    step_rows: Sequence[Mapping[str, Any]],
    events_by_rank: Mapping[str, Sequence[NormalizedEvent]],
    layers: Sequence[Any],
    row_indexes: Mapping[str, Sequence[int]],
) -> list[dict[str, Any]]:
    """Decompose each step into head / main / tail / bubble.

    The "main" window is the union span between the first and last layer
    boundaries inside a step segment. Anything before the first layer is
    "head" (warmup, batch metadata, host bookkeeping) and anything after the
    last layer is "tail" (cleanup, sampling, scheduling). All three windows
    are derived strictly from layer_segments.json -- no signature heuristics
    -- so the decomposition is traceable to evidence-grade boundaries.

    Steps without any layer boundary are emitted with the entire window
    booked as ``head_only`` and ``layer_count == 0`` so the report can flag
    them rather than silently dropping the data.
    """

    layers_by_step: dict[str, list[Any]] = defaultdict(list)
    for layer in layers:
        layers_by_step[str(layer.segment_id)].append(layer)
    for items in layers_by_step.values():
        items.sort(key=lambda layer: (float(layer.start_us), int(layer.row_start)))

    rows: list[dict[str, Any]] = []
    for step in step_rows:
        if step.get("segment_type") != "step":
            continue
        rank_id = str(step.get("rank_id") or "")
        rank_events = events_by_rank.get(rank_id, [])
        rank_rows = row_indexes.get(rank_id, [])
        step_row_start = int(step.get("row_start") or 0)
        step_row_end = int(step.get("row_end") or 0)
        step_wall = float(step.get("wall_ms") or 0.0)
        step_busy = float(step.get("busy_union_ms") or 0.0)
        step_bubble = float(step.get("underfeed_ms") or 0.0)
        layers_for_step = layers_by_step.get(str(step.get("segment_id") or ""), [])
        if not layers_for_step:
            head_metrics = _section_metrics(rank_events, rank_rows, step_row_start, step_row_end)
            row = {
                "segment_id": step.get("segment_id"),
                "rank_id": rank_id,
                "cluster_id": step.get("cluster_id"),
                "step_family": step.get("step_family"),
                "structure_signature": step.get("structure_signature"),
                "main_layer_count": step.get("main_layer_count"),
                "anatomy_kind": "head_only",
                "layer_count": 0,
                "step_wall_ms": step_wall,
                "step_busy_ms": step_busy,
                "step_bubble_ms": step_bubble,
                "head_row_start": head_metrics["row_start"],
                "head_row_end": head_metrics["row_end"],
                "head_start_us": head_metrics["start_us"],
                "head_end_us": head_metrics["end_us"],
                "head_wall_ms": head_metrics["wall_ms"],
                "head_busy_ms": head_metrics["busy_ms"],
                "head_bubble_ms": head_metrics["bubble_ms"],
                "head_event_count": head_metrics["event_count"],
                "main_row_start": None,
                "main_row_end": None,
                "main_start_us": None,
                "main_end_us": None,
                "main_wall_ms": 0.0,
                "main_busy_ms": 0.0,
                "main_bubble_ms": 0.0,
                "main_event_count": 0,
                "tail_row_start": None,
                "tail_row_end": None,
                "tail_start_us": None,
                "tail_end_us": None,
                "tail_wall_ms": 0.0,
                "tail_busy_ms": 0.0,
                "tail_bubble_ms": 0.0,
                "tail_event_count": 0,
                "head_ratio": 1.0 if step_wall > 0 else 0.0,
                "main_ratio": 0.0,
                "tail_ratio": 0.0,
                "bubble_ratio": (step_bubble / step_wall) if step_wall > 0 else 0.0,
            }
            rows.append(row)
            continue

        first_layer = layers_for_step[0]
        last_layer = layers_for_step[-1]
        main_row_start = int(first_layer.row_start)
        main_row_end = int(last_layer.row_end)
        head_row_end = main_row_start - 1
        tail_row_start = main_row_end + 1

        head_metrics = _section_metrics(rank_events, rank_rows, step_row_start, head_row_end)
        main_metrics = _section_metrics(rank_events, rank_rows, main_row_start, main_row_end)
        tail_metrics = _section_metrics(rank_events, rank_rows, tail_row_start, step_row_end)

        wall_total = head_metrics["wall_ms"] + main_metrics["wall_ms"] + tail_metrics["wall_ms"]
        denominator = step_wall if step_wall > 0 else (wall_total if wall_total > 0 else 0.0)
        head_ratio = (head_metrics["wall_ms"] / denominator) if denominator else 0.0
        main_ratio = (main_metrics["wall_ms"] / denominator) if denominator else 0.0
        tail_ratio = (tail_metrics["wall_ms"] / denominator) if denominator else 0.0
        bubble_ratio = (step_bubble / step_wall) if step_wall > 0 else 0.0

        row = {
            "segment_id": step.get("segment_id"),
            "rank_id": rank_id,
            "cluster_id": step.get("cluster_id"),
            "step_family": step.get("step_family"),
            "structure_signature": step.get("structure_signature"),
            "main_layer_count": step.get("main_layer_count"),
            "anatomy_kind": "full",
            "layer_count": len(layers_for_step),
            "step_wall_ms": step_wall,
            "step_busy_ms": step_busy,
            "step_bubble_ms": step_bubble,
            "head_row_start": head_metrics["row_start"],
            "head_row_end": head_metrics["row_end"],
            "head_start_us": head_metrics["start_us"],
            "head_end_us": head_metrics["end_us"],
            "head_wall_ms": head_metrics["wall_ms"],
            "head_busy_ms": head_metrics["busy_ms"],
            "head_bubble_ms": head_metrics["bubble_ms"],
            "head_event_count": head_metrics["event_count"],
            "main_row_start": main_metrics["row_start"],
            "main_row_end": main_metrics["row_end"],
            "main_start_us": main_metrics["start_us"],
            "main_end_us": main_metrics["end_us"],
            "main_wall_ms": main_metrics["wall_ms"],
            "main_busy_ms": main_metrics["busy_ms"],
            "main_bubble_ms": main_metrics["bubble_ms"],
            "main_event_count": main_metrics["event_count"],
            "tail_row_start": tail_metrics["row_start"],
            "tail_row_end": tail_metrics["row_end"],
            "tail_start_us": tail_metrics["start_us"],
            "tail_end_us": tail_metrics["end_us"],
            "tail_wall_ms": tail_metrics["wall_ms"],
            "tail_busy_ms": tail_metrics["busy_ms"],
            "tail_bubble_ms": tail_metrics["bubble_ms"],
            "tail_event_count": tail_metrics["event_count"],
            "head_ratio": round(head_ratio, 6),
            "main_ratio": round(main_ratio, 6),
            "tail_ratio": round(tail_ratio, 6),
            "bubble_ratio": round(bubble_ratio, 6),
        }
        rows.append(row)
    return rows


def attach_anatomy_to_step_rows(
    step_rows: list[dict[str, Any]], anatomy_rows: Sequence[Mapping[str, Any]]
) -> None:
    """Inline the anatomy summary into step_summary.csv for convenience.

    The full row-range evidence stays in ``step_anatomy.csv``; the inlined
    columns are intentionally a small subset (wall_ms / busy_ms per part +
    ratios) so step_summary.csv stays scrollable in spreadsheets.
    """

    by_id = {str(item.get("segment_id")): item for item in anatomy_rows}
    inline_keys = (
        "anatomy_kind",
        "head_wall_ms",
        "main_wall_ms",
        "tail_wall_ms",
        "head_busy_ms",
        "main_busy_ms",
        "tail_busy_ms",
        "head_bubble_ms",
        "main_bubble_ms",
        "tail_bubble_ms",
        "head_ratio",
        "main_ratio",
        "tail_ratio",
        "bubble_ratio",
    )
    for row in step_rows:
        anatomy = by_id.get(str(row.get("segment_id")))
        if anatomy is None:
            for key in inline_keys:
                row.setdefault(key, None)
            continue
        for key in inline_keys:
            row[key] = anatomy.get(key)


def rank_summary_rows(events_by_rank: Mapping[str, Sequence[NormalizedEvent]], segments: Sequence[Any]) -> list[dict[str, Any]]:
    segments_by_rank: dict[str, list[Any]] = defaultdict(list)
    for segment in segments:
        segments_by_rank[segment.rank_id].append(segment)
    rows: list[dict[str, Any]] = []
    for rank_id, events in events_by_rank.items():
        metrics = metrics_for_events(events, top_gap_limit=0)
        role_counts = Counter(role for event in events for role in event.op_roles)
        layer_inventory = sorted(
            {
                segment.main_layer_count
                for segment in segments_by_rank.get(rank_id, [])
                if segment.segment_type == "step" and segment.main_layer_count is not None
            }
        )
        rows.append(
            {
                "rank_id": rank_id,
                "step_count": sum(1 for segment in segments_by_rank.get(rank_id, []) if segment.segment_type == "step"),
                "segment_count": len(segments_by_rank.get(rank_id, [])),
                "layer_count_inventory": layer_inventory,
                "has_attention": bool(role_counts.get("attention")),
                "has_moe": bool(role_counts.get("moe")),
                "has_communication": bool(role_counts.get("communication")),
                "role_counts": dict(sorted(role_counts.items())),
                **metrics,
            }
        )
    return rows


def step_summary_rows(events_by_rank: Mapping[str, Sequence[NormalizedEvent]], segments: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    row_indexes = row_indexes_by_rank(events_by_rank)
    for segment in segments:
        rank_events = events_by_rank.get(segment.rank_id, [])
        events = event_slice(rank_events, row_indexes.get(segment.rank_id, []), segment.row_start, segment.row_end)
        metrics = metrics_for_events(events, top_gap_limit=5)
        role_counts = Counter(role for event in events for role in event.op_roles)
        category_counts = Counter(category for event in events for category in event.op_categories)
        rows.append(
            {
                "segment_id": segment.segment_id,
                "rank_id": segment.rank_id,
                "segment_type": segment.segment_type,
                "complete": segment.complete,
                "cluster_id": segment.cluster_id,
                "step_family": segment.step_family,
                "row_start": segment.row_start,
                "row_end": segment.row_end,
                "main_layer_count": segment.main_layer_count,
                "speculative_layer_count": segment.speculative_layer_count,
                "structure_signature": segment.structure_signature,
                "has_attention": bool(role_counts.get("attention")),
                "has_moe": bool(role_counts.get("moe")),
                "has_communication": bool(role_counts.get("communication")),
                "attention_event_count": role_counts.get("attention", 0),
                "moe_event_count": role_counts.get("moe", 0),
                "comm_event_count": role_counts.get("communication", 0),
                "category_counts": dict(sorted(category_counts.items())),
                "anomaly_tags": anomaly_tags(metrics),
                "top_bubbles": metrics.get("top_bubbles", []),
                "evidence_ids": list(segment.evidence_ids),
                **{key: value for key, value in metrics.items() if key != "top_bubbles"},
            }
        )
    return rows


def layer_summary_rows(events_by_rank: Mapping[str, Sequence[NormalizedEvent]], layers: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    row_indexes = row_indexes_by_rank(events_by_rank)
    for layer in layers:
        rank_events = events_by_rank.get(layer.rank_id, [])
        events = event_slice(rank_events, row_indexes.get(layer.rank_id, []), layer.row_start, layer.row_end)
        metrics = metrics_for_events(events, top_gap_limit=0)
        role_counts = Counter(role for event in events for role in event.op_roles)
        rows.append(
            {
                "layer_id": layer.layer_id,
                "segment_id": layer.segment_id,
                "rank_id": layer.rank_id,
                "layer_index": layer.layer_index,
                "layer_role": layer.layer_role,
                "boundary_source": layer.boundary_source,
                "row_start": layer.row_start,
                "row_end": layer.row_end,
                "structure_signature": layer.structure_signature,
                "has_attention": bool(role_counts.get("attention")),
                "has_moe": bool(role_counts.get("moe")),
                "has_communication": bool(role_counts.get("communication")),
                "role_counts": dict(sorted(role_counts.items())),
                "evidence_ids": list(layer.evidence_ids),
                **metrics,
            }
        )
    return rows


def operator_summary_rows(events: Sequence[NormalizedEvent], max_sample_rows: int = 16) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        role_key = ",".join(event.op_roles) or "unknown"
        grouped[(event.rank_id, event.name_raw, event.task_type, role_key)].append(event)
    rows: list[dict[str, Any]] = []
    for (rank_id, name, task, role_key), items in grouped.items():
        duration = sum(event.duration_us for event in items)
        wait = sum(event.wait_us for event in items)
        total = duration + wait
        pipeline_aggregate = sum_pipeline_breakdown(event.pipeline_us for event in items)
        op_type_counter = Counter(event.op_type for event in items if event.op_type)
        op_type = op_type_counter.most_common(1)[0][0] if op_type_counter else "unknown"
        is_aicpu = any(is_aicpu_event(event) for event in items)
        is_communication = any(is_comm_event(event) for event in items)
        bound = bound_class_from_pipeline(
            pipeline_aggregate or None,
            op_type=op_type,
            is_aicpu=is_aicpu,
            is_communication=is_communication,
        )
        pipeline_signal = bool(pipeline_aggregate)
        row = {
            "rank_id": rank_id,
            "name": name,
            "task_type": task,
            "roles": role_key,
            "categories": sorted({category for event in items for category in event.op_categories}),
            "call_count": len(items),
            "duration_sum_us": round(duration, 3),
            "wait_sum_us": round(wait, 3),
            "total_cost_sum_us": round(total, 3),
            "duration_avg_us": round(duration / len(items), 6),
            "wait_avg_us": round(wait / len(items), 6),
            "total_cost_avg_us": round(total / len(items), 6),
            "wait_ratio": round(wait / total, 6) if total > 0 else 0.0,
            "stream_count": len({event.stream_id for event in items}),
            "row_ranges": row_ranges(event.row_idx for event in items),
            "sample_rows": sorted(event.row_idx for event in items)[:max_sample_rows],
            "sample_event_ids": [event.event_id for event in sorted(items, key=lambda item: item.row_idx)[:max_sample_rows]],
            "op_type": op_type,
            "bound_stage": bound["bound_stage"],
            "bound_family": bound["bound_family"],
            "dominant_core": bound["dominant_core"],
            "pipeline_signal": pipeline_signal,
        }
        for key in PIPELINE_FIELDS:
            row[key] = pipeline_aggregate.get(key) if pipeline_aggregate else None
        rows.append(row)
    rows.sort(key=lambda item: (item["rank_id"], -float(item["total_cost_sum_us"]), item["name"]))
    return rows


_HCCL_OP_KIND_BY_TASK: dict[str, str] = {
    "HCOM_ALLREDUCE_": "allreduce",
    "HCOM_ALLGATHER_": "allgather",
    "HCOM_REDUCESCATTER_": "reducescatter",
    "HCOM_ALLTOALLV_": "alltoallv",
    "HCOM_ALLTOALLVC_": "alltoallv",
    "HCOM_BROADCAST_": "broadcast",
    "HCOM_SEND_": "send_recv",
    "HCOM_RECEIVE_": "send_recv",
    "HCOM_BARRIER_": "barrier",
    "HCCL_BATCHPUT_": "send_recv",
    "HCCL_BATCHSENDRECV_": "send_recv",
}


def _hccl_op_kind_from_task(task_type: str) -> str:
    """Map a HCCL ``task_type`` to a canonical ``hccl_op_kind``.

    See ``knowledge/communication_taxonomy.md`` § 2 for the source of
    truth.  Anything that is HCOM-prefixed but not in the table maps to
    ``other`` so the report flags it instead of silently dropping it.
    """

    upper = (task_type or "").upper()
    direct = _HCCL_OP_KIND_BY_TASK.get(upper)
    if direct is not None:
        return direct
    if upper.startswith("HCOM_") or upper.startswith("HCCL_"):
        return "other"
    return "other"


def operator_class_summary_rows(
    operator_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge per-rank operator_summary rows into rank-merged class rows.

    Group key is ``(name, task_type, op_type, roles)``.  For each group
    we sum ``duration``/``wait``/``call_count`` and the 11 pipeline
    stages, then re-derive ``bound_stage`` / ``bound_family`` /
    ``dominant_core`` from the summed pipeline so the bound classification
    reflects the aggregate behaviour rather than a mean-of-labels.

    Per-rank duration spread is kept as ``rank_duration_min/max/p50/skew_ratio``
    so the report can flag operators with imbalanced rank distribution
    even though the whole row is rank-merged.
    """

    grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in operator_rows:
        key = (
            str(row.get("name") or ""),
            str(row.get("task_type") or ""),
            str(row.get("op_type") or ""),
            str(row.get("roles") or ""),
        )
        grouped[key].append(row)

    rows: list[dict[str, Any]] = []
    for (name, task, op_type, roles), members in grouped.items():
        durations = [float(item.get("duration_sum_us") or 0.0) for item in members]
        waits = [float(item.get("wait_sum_us") or 0.0) for item in members]
        call_counts = [int(item.get("call_count") or 0) for item in members]
        duration_total = sum(durations)
        wait_total = sum(waits)
        total_cost = duration_total + wait_total
        call_total = sum(call_counts)
        pipeline_sum: dict[str, float] = {key: 0.0 for key in PIPELINE_FIELDS}
        any_pipeline = False
        for item in members:
            for field in PIPELINE_FIELDS:
                value = item.get(field)
                if value is None or value == "":
                    continue
                try:
                    pipeline_sum[field] += float(value)
                    any_pipeline = True
                except (TypeError, ValueError):
                    continue
        bound = bound_class_from_pipeline(
            pipeline_sum if any_pipeline else None,
            op_type=op_type or None,
            is_aicpu=op_type == "aicpu",
            is_communication=op_type in {"communication", "mix_comm_aiv"} and not any_pipeline,
        )
        rank_durations = sorted(durations)
        if rank_durations:
            min_dur = rank_durations[0]
            max_dur = rank_durations[-1]
            mean_dur = duration_total / len(rank_durations)
            skew_ratio = ((max_dur - min_dur) / mean_dur) if mean_dur > 0 else 0.0
            p50 = quantile(rank_durations, 0.5)
        else:
            min_dur = max_dur = mean_dur = skew_ratio = p50 = 0.0
        row = {
            "name": name,
            "task_type": task,
            "op_type": op_type,
            "roles": roles,
            "rank_count": len({str(item.get("rank_id") or "") for item in members}),
            "call_count": call_total,
            "duration_sum_us": round(duration_total, 3),
            "wait_sum_us": round(wait_total, 3),
            "total_cost_sum_us": round(total_cost, 3),
            "duration_avg_us": round(duration_total / call_total, 6) if call_total else 0.0,
            "wait_avg_us": round(wait_total / call_total, 6) if call_total else 0.0,
            "wait_ratio": round(wait_total / total_cost, 6) if total_cost > 0 else 0.0,
            "rank_duration_min_us": round(min_dur, 3),
            "rank_duration_max_us": round(max_dur, 3),
            "rank_duration_p50_us": round(p50, 3),
            "rank_duration_skew_ratio": round(skew_ratio, 6),
            "bound_stage": bound["bound_stage"],
            "bound_family": bound["bound_family"],
            "dominant_core": bound["dominant_core"],
            "pipeline_signal": any_pipeline,
        }
        for field in PIPELINE_FIELDS:
            row[field] = round(pipeline_sum[field], 6) if any_pipeline else None
        rows.append(row)
    rows.sort(key=lambda item: -float(item["total_cost_sum_us"]))
    return rows


def hccl_op_summary_rows(
    operator_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate HCCL collectives per ``(hccl_op_kind, comm_aiv_fused, rank_id)``.

    HCCL operator names carry a per-call counter suffix
    (``hcom_allReduce__800_2322_1``), so per-name aggregation is too
    granular to be useful.  We instead group by the canonical
    ``hccl_op_kind`` and per-rank to expose total wall, call count,
    average duration, p50/p90/max duration, and total wait per rank.

    The same kind appears twice when both pure-comm and ``mix_comm_aiv``
    rows exist (e.g. ``allreduce`` and ``allreduce`` + comm_aiv_fused),
    so we keep ``comm_aiv_fused`` as part of the row key rather than
    collapsing it.
    """

    grouped: dict[tuple[str, bool, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in operator_rows:
        op_type = str(row.get("op_type") or "")
        if op_type not in {"communication", "mix_comm_aiv"}:
            continue
        kind = _hccl_op_kind_from_task(str(row.get("task_type") or ""))
        comm_aiv_fused = op_type == "mix_comm_aiv"
        rank_id = str(row.get("rank_id") or "")
        grouped[(kind, comm_aiv_fused, rank_id)].append(row)

    rows: list[dict[str, Any]] = []
    for (kind, comm_aiv_fused, rank_id), members in grouped.items():
        call_total = sum(int(item.get("call_count") or 0) for item in members)
        if call_total <= 0:
            continue
        duration_total = sum(float(item.get("duration_sum_us") or 0.0) for item in members)
        wait_total = sum(float(item.get("wait_sum_us") or 0.0) for item in members)
        # Use ``duration_avg_us`` from each per-name aggregate as the
        # per-call sample so the percentile reflects per-call behaviour
        # (every per-name row in operator_summary.csv represents a single
        # collective invocation in practice because the trailing counter
        # suffix makes the names unique).
        per_call_durations = sorted(
            float(item.get("duration_avg_us") or 0.0) for item in members
        )
        if per_call_durations:
            duration_p50 = quantile(per_call_durations, 0.5)
            duration_p90 = quantile(per_call_durations, 0.9)
            duration_max = per_call_durations[-1]
            duration_min = per_call_durations[0]
        else:
            duration_p50 = duration_p90 = duration_max = duration_min = 0.0
        rows.append(
            {
                "hccl_op_kind": kind,
                "comm_aiv_fused": comm_aiv_fused,
                "rank_id": rank_id,
                "name_count": len(members),
                "call_count": call_total,
                "duration_sum_us": round(duration_total, 3),
                "wait_sum_us": round(wait_total, 3),
                "duration_avg_us": round(duration_total / call_total, 6),
                "duration_min_us": round(duration_min, 3),
                "duration_p50_us": round(duration_p50, 3),
                "duration_p90_us": round(duration_p90, 3),
                "duration_max_us": round(duration_max, 3),
                "wait_ratio": round(wait_total / (duration_total + wait_total), 6) if (duration_total + wait_total) > 0 else 0.0,
            }
        )
    rows.sort(
        key=lambda item: (
            item["hccl_op_kind"],
            not item["comm_aiv_fused"],
            -float(item["duration_sum_us"]),
            item["rank_id"],
        )
    )
    return rows


def hccl_class_summary_rows(
    hccl_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Roll up per-rank HCCL aggregates into per-kind cross-rank rows.

    Provides a single line per ``(hccl_op_kind, comm_aiv_fused)`` with
    rank-merged totals plus ``rank_skew_ratio`` (max - min duration_avg
    over mean) so the report can flag the slowest collective family.
    """

    grouped: dict[tuple[str, bool], list[Mapping[str, Any]]] = defaultdict(list)
    for row in hccl_rows:
        kind = str(row.get("hccl_op_kind") or "")
        fused = bool(row.get("comm_aiv_fused"))
        grouped[(kind, fused)].append(row)

    rows: list[dict[str, Any]] = []
    for (kind, fused), members in grouped.items():
        call_total = sum(int(item.get("call_count") or 0) for item in members)
        duration_total = sum(float(item.get("duration_sum_us") or 0.0) for item in members)
        wait_total = sum(float(item.get("wait_sum_us") or 0.0) for item in members)
        per_rank_avgs = [float(item.get("duration_avg_us") or 0.0) for item in members]
        per_rank_avgs.sort()
        if per_rank_avgs:
            avg_min = per_rank_avgs[0]
            avg_max = per_rank_avgs[-1]
            avg_mean = sum(per_rank_avgs) / len(per_rank_avgs)
            skew_ratio = ((avg_max - avg_min) / avg_mean) if avg_mean > 0 else 0.0
        else:
            avg_min = avg_max = avg_mean = skew_ratio = 0.0
        max_duration_per_call = max(
            (float(item.get("duration_max_us") or 0.0) for item in members),
            default=0.0,
        )
        rows.append(
            {
                "hccl_op_kind": kind,
                "comm_aiv_fused": fused,
                "rank_count": len({str(item.get("rank_id") or "") for item in members}),
                "call_count": call_total,
                "duration_sum_us": round(duration_total, 3),
                "wait_sum_us": round(wait_total, 3),
                "duration_avg_us": round(duration_total / call_total, 6) if call_total else 0.0,
                "rank_avg_min_us": round(avg_min, 3),
                "rank_avg_max_us": round(avg_max, 3),
                "rank_avg_mean_us": round(avg_mean, 6),
                "rank_skew_ratio": round(skew_ratio, 6),
                "duration_max_us": round(max_duration_per_call, 3),
            }
        )
    rows.sort(
        key=lambda item: (
            -float(item["duration_sum_us"]),
            item["hccl_op_kind"],
            not item["comm_aiv_fused"],
        )
    )
    return rows


def wait_anchor_rows(operator_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_rank: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in operator_rows:
        by_rank[str(row.get("rank_id"))].append(row)
    for rank_id, items in by_rank.items():
        ranked = sorted(items, key=lambda item: float(item.get("total_cost_avg_us") or 0.0), reverse=True)
        for rank, row in enumerate(ranked, 1):
            duration_avg = float(row.get("duration_avg_us") or 0.0)
            wait_ratio = float(row.get("wait_ratio") or 0.0)
            if wait_ratio > WAIT_ANCHOR_RATIO:
                item = dict(row)
                item["total_cost_avg_rank"] = rank
                item["is_false_hotspot_risk"] = (
                    wait_ratio > FALSE_HOTSPOT_WAIT_RATIO
                    and duration_avg < FALSE_HOTSPOT_DURATION_US
                    and rank <= FALSE_HOTSPOT_TOP_RANK
                )
                rows.append(item)
    return rows


def overlap_sum(targets: Sequence[Interval], masks: Sequence[Interval]) -> float:
    ordered_targets = sorted(targets, key=lambda item: (item.start_us, item.end_us))
    ordered_masks = sorted(masks, key=lambda item: (item.start_us, item.end_us))
    total = 0.0
    pos = 0
    for target in ordered_targets:
        while pos < len(ordered_masks) and ordered_masks[pos].end_us <= target.start_us:
            pos += 1
        probe = pos
        while probe < len(ordered_masks) and ordered_masks[probe].start_us < target.end_us:
            mask = ordered_masks[probe]
            left = max(target.start_us, mask.start_us)
            right = min(target.end_us, mask.end_us)
            if right > left:
                total += right - left
            probe += 1
    return total


def aicpu_rows(events_by_rank: Mapping[str, Sequence[NormalizedEvent]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank_id, events in events_by_rank.items():
        ai_core = [Interval(event.start_us, event.end_us) for event in events if is_ai_core_like(event)]
        grouped: dict[str, list[NormalizedEvent]] = defaultdict(list)
        for event in events:
            if is_aicpu_event(event):
                grouped[event.name_raw].append(event)
        for name, items in grouped.items():
            duration = sum(event.duration_us for event in items)
            overlapped = overlap_sum([Interval(event.start_us, event.end_us) for event in items], ai_core)
            ratio = overlapped / duration if duration > 0 else 0.0
            if ratio >= AICPU_MASKED_RATIO:
                classification = "AICPU_MASKED_BUT_UNDESIRABLE"
            elif ratio >= AICPU_PARTIAL_RATIO:
                classification = "AICPU_PARTIALLY_EXPOSED"
            else:
                classification = "AICPU_EXPOSED_NOT_ALLOWED"
            rows.append(
                {
                    "rank_id": rank_id,
                    "name": name,
                    "call_count": len(items),
                    "duration_sum_us": round(duration, 3),
                    "overlap_with_ai_core_us": round(overlapped, 3),
                    "masked_ratio": round(ratio, 6),
                    "classification": classification,
                    "row_ranges": row_ranges(event.row_idx for event in items),
                    "sample_event_ids": [event.event_id for event in items[:16]],
                }
            )
    rows.sort(key=lambda item: (item["rank_id"], -float(item["duration_sum_us"]), item["name"]))
    return rows


def bubble_evidence_rows(step_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in step_rows:
        if step.get("segment_type") != "step":
            continue
        for bubble in step.get("top_bubbles") or []:
            evidence_id = stable_id("evd", step.get("segment_id"), "bubble", bubble.get("bubble_index"), bubble.get("start_us"))
            rows.append(
                {
                    "evidence_id": evidence_id,
                    "rank_id": step.get("rank_id"),
                    "segment_id": step.get("segment_id"),
                    "cluster_id": step.get("cluster_id"),
                    "step_family": step.get("step_family"),
                    **bubble,
                }
            )
    rows.sort(key=lambda item: float(item.get("duration_us") or 0.0), reverse=True)
    return rows


def block_summary_rows(
    events_by_rank: Mapping[str, Sequence[NormalizedEvent]],
    blocks: Sequence[BlockSegment],
    block_class_by_id: Mapping[str, str],
) -> list[dict[str, Any]]:
    """One row per block; mirrors layer_summary.csv with block_kind columns.

    Each row carries the per-block wall / busy / bubble metrics, the
    pipeline-aggregated stage breakdown and bound classification, plus
    the top-5 operator cost contributors *inside the block* so the
    report can immediately answer "which kernel dominates this block".
    """

    rows: list[dict[str, Any]] = []
    row_indexes = row_indexes_by_rank(events_by_rank)
    for block in blocks:
        rank_events = events_by_rank.get(block.rank_id, [])
        events = event_slice(rank_events, row_indexes.get(block.rank_id, []), block.row_start, block.row_end)
        metrics = metrics_for_events(events, top_gap_limit=0)
        role_counts = Counter(role for event in events for role in event.op_roles)
        op_type_counts: Counter[str] = Counter()
        op_type_duration: dict[str, float] = defaultdict(float)
        comm_duration_us = 0.0
        block_duration_us = 0.0
        for event in events:
            op_type = event.op_type or "unknown"
            op_type_counts[op_type] += 1
            op_type_duration[op_type] += float(event.duration_us)
            block_duration_us += float(event.duration_us)
            if op_type in {"communication", "mix_comm_aiv"}:
                comm_duration_us += float(event.duration_us)
        pipeline_agg = sum_pipeline_breakdown(event.pipeline_us for event in events)
        if op_type_duration:
            dominant_op_type = max(op_type_duration.items(), key=lambda item: item[1])[0]
        else:
            dominant_op_type = "unknown"
        # Block-level bound classification analyses the **AI-Core stage**
        # bottleneck.  When the block has *any* compute pipeline signal,
        # we deliberately bypass the communication / aicpu short-circuit
        # so a MoE block that spends 50 % of wall time in alltoall_v
        # still surfaces the cube vs vector breakdown of the remaining
        # 50 %.  Comm coverage is reported separately via ``comm_share``
        # so consumers can swap lenses.
        if pipeline_agg:
            bound = bound_class_from_pipeline(
                pipeline_agg,
                op_type=None,
                is_aicpu=False,
                is_communication=False,
            )
        else:
            bound = bound_class_from_pipeline(
                None,
                op_type=dominant_op_type,
                is_aicpu=dominant_op_type == "aicpu",
                is_communication=dominant_op_type == "communication",
            )
        comm_share = round(comm_duration_us / block_duration_us, 6) if block_duration_us > 0 else 0.0
        op_cost: dict[tuple[str, str], float] = defaultdict(float)
        op_calls: dict[tuple[str, str], int] = defaultdict(int)
        for event in events:
            key = (event.name_raw, event.task_type)
            op_cost[key] += float(event.duration_us)
            op_calls[key] += 1
        top_ops = [
            {
                "name": name,
                "task_type": task,
                "duration_sum_us": round(cost, 3),
                "call_count": op_calls[(name, task)],
            }
            for (name, task), cost in sorted(op_cost.items(), key=lambda item: item[1], reverse=True)[:5]
        ]
        row = {
            "block_id": block.block_id,
            "block_class_id": block_class_by_id.get(block.block_id),
            "layer_id": block.layer_id,
            "segment_id": block.segment_id,
            "rank_id": block.rank_id,
            "layer_index": block.layer_index,
            "block_index": block.block_index,
            "block_kind": block.block_kind,
            "companion_layer": block.companion_layer,
            "row_start": block.row_start,
            "row_end": block.row_end,
            "start_us": block.start_us,
            "end_us": block.end_us,
            "event_count": len(events),
            "has_attention": bool(role_counts.get("attention") or role_counts.get("attention_aux")),
            "has_moe": bool(role_counts.get("moe")),
            "has_communication": bool(role_counts.get("communication")),
            "role_counts": dict(sorted(role_counts.items())),
            "op_type_counts": dict(sorted(op_type_counts.items())),
            "bound_stage": bound["bound_stage"],
            "bound_family": bound["bound_family"],
            "dominant_core": bound["dominant_core"],
            "dominant_op_type": dominant_op_type,
            "pipeline_signal": bool(pipeline_agg),
            "comm_duration_us": round(comm_duration_us, 3),
            "comm_share": comm_share,
            "top_ops": top_ops,
            **{key: value for key, value in metrics.items() if key != "top_bubbles"},
        }
        for key in PIPELINE_FIELDS:
            row[key] = pipeline_agg.get(key) if pipeline_agg else None
        rows.append(row)
    return rows


def _aggregate_top_ops(
    op_costs: Mapping[tuple[str, str], dict[str, float]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Roll a name -> {duration, call_count} dict into a sorted top-N list."""

    items = sorted(op_costs.items(), key=lambda item: item[1].get("duration_sum_us", 0.0), reverse=True)
    return [
        {
            "name": name,
            "task_type": task,
            "duration_sum_us": round(stats.get("duration_sum_us", 0.0), 3),
            "call_count": int(stats.get("call_count", 0)),
        }
        for (name, task), stats in items[:limit]
    ]


def block_class_summary_rows(
    block_rows: Sequence[Mapping[str, Any]],
    block_classes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Roll up per-block rows into per-block-class aggregates.

    Pipeline aggregates and bound classification are recomputed on the
    *class-level* sum, never derived from per-member ratios -- that way
    a class's bound family reflects the overall behaviour rather than a
    misleading mean of bound labels.
    """

    by_class: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in block_rows:
        cid = row.get("block_class_id")
        if not cid:
            continue
        by_class[str(cid)].append(row)

    rows: list[dict[str, Any]] = []
    for class_id, members in by_class.items():
        class_meta = block_classes.get(class_id, {})
        wall_values = [float(item.get("wall_ms") or 0.0) for item in members]
        busy_values = [float(item.get("busy_union_ms") or 0.0) for item in members]
        bubble_values = [float(item.get("underfeed_ms") or 0.0) for item in members]
        comm_share_values = [float(item.get("comm_share") or 0.0) for item in members]
        comm_duration_values = [float(item.get("comm_duration_us") or 0.0) for item in members]
        pipeline_sum: dict[str, float] = {key: 0.0 for key in PIPELINE_FIELDS}
        any_pipeline = False
        for item in members:
            for key in PIPELINE_FIELDS:
                value = item.get(key)
                if value is None:
                    continue
                pipeline_sum[key] += float(value)
                any_pipeline = True
        op_costs: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"duration_sum_us": 0.0, "call_count": 0})
        for item in members:
            for op in item.get("top_ops") or []:
                key = (str(op.get("name") or ""), str(op.get("task_type") or ""))
                op_costs[key]["duration_sum_us"] += float(op.get("duration_sum_us") or 0.0)
                op_costs[key]["call_count"] += int(op.get("call_count") or 0)
        op_type_counts: Counter[str] = Counter()
        for item in members:
            for op_type, count in (item.get("op_type_counts") or {}).items():
                op_type_counts[str(op_type)] += int(count)
        bound_family_counts: Counter[str] = Counter(str(item.get("bound_family") or "unknown") for item in members)
        sample_kind = members[0].get("block_kind") if members else class_meta.get("block_kind")
        companion = bool(class_meta.get("companion_layer", any(item.get("companion_layer") for item in members)))
        # Same compute-first lens as block_summary_rows: when any
        # member contributed pipeline signal, classify by the aggregate
        # pipeline (cube vs vector vs MTE) and let comm-side stats live
        # in the histogram.  Otherwise fall back to dominant op_type.
        dominant_op_type = op_type_counts.most_common(1)[0][0] if op_type_counts else "unknown"
        if any_pipeline:
            bound = bound_class_from_pipeline(
                pipeline_sum,
                op_type=None,
                is_aicpu=False,
                is_communication=False,
            )
        else:
            bound = bound_class_from_pipeline(
                None,
                op_type=dominant_op_type,
                is_aicpu=dominant_op_type == "aicpu",
                is_communication=dominant_op_type == "communication",
            )
        rank_count = len({str(item.get("rank_id")) for item in members})
        row = {
            "block_class_id": class_id,
            "block_kind": sample_kind,
            "companion_layer": companion,
            "member_count": len(members),
            "rank_count": rank_count,
            "has_unknown_shape": bool(class_meta.get("has_unknown_shape", False)),
            "wall_ms_sum": round(sum(wall_values), 3),
            "wall_ms_mean": round(statistics_mean(wall_values), 6),
            "wall_ms_p50": round(quantile(wall_values, 0.5), 6),
            "wall_ms_p90": round(quantile(wall_values, 0.9), 6),
            "busy_ms_sum": round(sum(busy_values), 3),
            "busy_ms_mean": round(statistics_mean(busy_values), 6),
            "bubble_ms_mean": round(statistics_mean(bubble_values), 6),
            "comm_share_mean": round(statistics_mean(comm_share_values), 6),
            "comm_duration_us_sum": round(sum(comm_duration_values), 3),
            "pipeline_signal": any_pipeline,
            "bound_stage": bound["bound_stage"],
            "bound_family": bound["bound_family"],
            "dominant_core": bound["dominant_core"],
            "dominant_op_type": dominant_op_type,
            "bound_family_member_histogram": dict(sorted(bound_family_counts.items())),
            "op_type_counts": dict(sorted(op_type_counts.items())),
            "top_ops": _aggregate_top_ops(op_costs, limit=10),
        }
        for key in PIPELINE_FIELDS:
            row[key] = round(pipeline_sum[key], 6) if any_pipeline else None
        rows.append(row)
    rows.sort(key=lambda item: (-int(item.get("member_count") or 0), str(item.get("block_kind") or "")))
    return rows


def layer_class_summary_rows(
    layer_rows: Sequence[Mapping[str, Any]],
    layer_classes: Mapping[str, Mapping[str, Any]],
    block_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Roll up per-layer rows into per-layer-class aggregates.

    The block-kind wall-ms breakdown lets the report show "attention
    takes X%, moe takes Y%" inside a class without drilling into the
    block_summary CSV.
    """

    layer_class_by_id: dict[str, str] = {}
    for class_id, info in layer_classes.items():
        for layer_id in info.get("members") or ():
            layer_class_by_id[str(layer_id)] = class_id

    blocks_by_layer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for block in block_rows:
        blocks_by_layer[str(block.get("layer_id"))].append(block)

    by_class: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for layer in layer_rows:
        cid = layer_class_by_id.get(str(layer.get("layer_id")))
        if not cid:
            continue
        by_class[cid].append(layer)

    rows: list[dict[str, Any]] = []
    for class_id, members in by_class.items():
        class_meta = layer_classes.get(class_id, {})
        wall_values = [float(item.get("wall_ms") or 0.0) for item in members]
        busy_values = [float(item.get("busy_union_ms") or 0.0) for item in members]
        bubble_values = [float(item.get("underfeed_ms") or 0.0) for item in members]
        block_kind_wall: dict[str, list[float]] = defaultdict(list)
        op_costs: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"duration_sum_us": 0.0, "call_count": 0})
        for layer in members:
            for block in blocks_by_layer.get(str(layer.get("layer_id")), []):
                kind = str(block.get("block_kind") or "other")
                block_kind_wall[kind].append(float(block.get("wall_ms") or 0.0))
                for op in block.get("top_ops") or []:
                    key = (str(op.get("name") or ""), str(op.get("task_type") or ""))
                    op_costs[key]["duration_sum_us"] += float(op.get("duration_sum_us") or 0.0)
                    op_costs[key]["call_count"] += int(op.get("call_count") or 0)
        rank_count = len({str(item.get("rank_id")) for item in members})
        row = {
            "layer_class_id": class_id,
            "structure_signature": class_meta.get("structure_signature"),
            "block_kinds": list(class_meta.get("block_kinds") or []),
            "block_class_ids": list(class_meta.get("block_class_ids") or []),
            "companion_layer": bool(class_meta.get("companion_layer", False)),
            "has_unknown_shape": bool(class_meta.get("has_unknown_shape", False)),
            "member_count": len(members),
            "rank_count": rank_count,
            "wall_ms_sum": round(sum(wall_values), 3),
            "wall_ms_mean": round(statistics_mean(wall_values), 6),
            "wall_ms_p50": round(quantile(wall_values, 0.5), 6),
            "wall_ms_p90": round(quantile(wall_values, 0.9), 6),
            "busy_ms_mean": round(statistics_mean(busy_values), 6),
            "bubble_ms_mean": round(statistics_mean(bubble_values), 6),
            "block_kind_wall_ms_mean": {kind: round(statistics_mean(values), 6) for kind, values in block_kind_wall.items()},
            "block_kind_wall_ms_share_mean": _ratio_dict(block_kind_wall, wall_values),
            "top_ops": _aggregate_top_ops(op_costs, limit=10),
        }
        rows.append(row)
    rows.sort(key=lambda item: (-int(item.get("member_count") or 0), str(item.get("layer_class_id") or "")))
    return rows


def step_class_summary_rows(
    step_rows: Sequence[Mapping[str, Any]],
    anatomy_rows: Sequence[Mapping[str, Any]],
    step_classes: Mapping[str, Mapping[str, Any]],
    layer_rows: Sequence[Mapping[str, Any]],
    block_rows: Sequence[Mapping[str, Any]],
    layer_class_by_id: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Roll up per-step rows into per-step-class aggregates.

    Combines the step_summary metrics with the head/main/tail/bubble
    decomposition from step_anatomy, plus a top-N layer-class ranking
    so the report can answer "which layers dominate this step class".
    """

    step_class_by_id: dict[str, str] = {}
    for class_id, info in step_classes.items():
        for segment_id in info.get("members") or ():
            step_class_by_id[str(segment_id)] = class_id

    anatomy_by_segment: dict[str, Mapping[str, Any]] = {str(item.get("segment_id")): item for item in anatomy_rows}
    layers_by_segment: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for layer in layer_rows:
        layers_by_segment[str(layer.get("segment_id"))].append(layer)
    blocks_by_layer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for block in block_rows:
        blocks_by_layer[str(block.get("layer_id"))].append(block)

    by_class: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for step in step_rows:
        if step.get("segment_type") != "step":
            continue
        cid = step_class_by_id.get(str(step.get("segment_id")))
        if not cid:
            continue
        by_class[cid].append(step)

    rows: list[dict[str, Any]] = []
    for class_id, members in by_class.items():
        class_meta = step_classes.get(class_id, {})
        wall_values = [float(item.get("wall_ms") or 0.0) for item in members]
        busy_values = [float(item.get("busy_union_ms") or 0.0) for item in members]
        bubble_values = [float(item.get("underfeed_ms") or 0.0) for item in members]
        head_values: list[float] = []
        main_values: list[float] = []
        tail_values: list[float] = []
        head_ratios: list[float] = []
        main_ratios: list[float] = []
        tail_ratios: list[float] = []
        bubble_ratios: list[float] = []
        layer_class_costs: dict[str, dict[str, float]] = defaultdict(lambda: {"wall_ms_sum": 0.0, "member_count": 0})
        op_costs: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"duration_sum_us": 0.0, "call_count": 0})
        for step in members:
            anatomy = anatomy_by_segment.get(str(step.get("segment_id")))
            if anatomy is not None:
                head_values.append(float(anatomy.get("head_wall_ms") or 0.0))
                main_values.append(float(anatomy.get("main_wall_ms") or 0.0))
                tail_values.append(float(anatomy.get("tail_wall_ms") or 0.0))
                head_ratios.append(float(anatomy.get("head_ratio") or 0.0))
                main_ratios.append(float(anatomy.get("main_ratio") or 0.0))
                tail_ratios.append(float(anatomy.get("tail_ratio") or 0.0))
                bubble_ratios.append(float(anatomy.get("bubble_ratio") or 0.0))
            for layer in layers_by_segment.get(str(step.get("segment_id")), []):
                lc = layer_class_by_id.get(str(layer.get("layer_id")))
                if lc:
                    bucket = layer_class_costs[lc]
                    bucket["wall_ms_sum"] += float(layer.get("wall_ms") or 0.0)
                    bucket["member_count"] += 1
                for block in blocks_by_layer.get(str(layer.get("layer_id")), []):
                    for op in block.get("top_ops") or []:
                        key = (str(op.get("name") or ""), str(op.get("task_type") or ""))
                        op_costs[key]["duration_sum_us"] += float(op.get("duration_sum_us") or 0.0)
                        op_costs[key]["call_count"] += int(op.get("call_count") or 0)
        top_layer_classes = sorted(
            layer_class_costs.items(),
            key=lambda item: item[1]["wall_ms_sum"],
            reverse=True,
        )[:5]
        rank_count = len({str(item.get("rank_id")) for item in members})
        row = {
            "step_class_id": class_id,
            "structure_signature": class_meta.get("structure_signature"),
            "step_family": class_meta.get("step_family"),
            "main_layer_count": class_meta.get("main_layer_count"),
            "layer_count": class_meta.get("layer_count"),
            "has_unknown_shape": bool(class_meta.get("has_unknown_shape", False)),
            "member_count": len(members),
            "rank_count": rank_count,
            "wall_ms_sum": round(sum(wall_values), 3),
            "wall_ms_mean": round(statistics_mean(wall_values), 6),
            "wall_ms_p50": round(quantile(wall_values, 0.5), 6),
            "wall_ms_p90": round(quantile(wall_values, 0.9), 6),
            "busy_ms_mean": round(statistics_mean(busy_values), 6),
            "bubble_ms_mean": round(statistics_mean(bubble_values), 6),
            "head_ms_mean": round(statistics_mean(head_values), 6),
            "main_ms_mean": round(statistics_mean(main_values), 6),
            "tail_ms_mean": round(statistics_mean(tail_values), 6),
            "head_ratio_mean": round(statistics_mean(head_ratios), 6),
            "main_ratio_mean": round(statistics_mean(main_ratios), 6),
            "tail_ratio_mean": round(statistics_mean(tail_ratios), 6),
            "bubble_ratio_mean": round(statistics_mean(bubble_ratios), 6),
            "top_layer_classes": [
                {
                    "layer_class_id": lc_id,
                    "wall_ms_sum": round(stats["wall_ms_sum"], 3),
                    "member_count": int(stats["member_count"]),
                }
                for lc_id, stats in top_layer_classes
            ],
            "top_ops": _aggregate_top_ops(op_costs, limit=10),
        }
        rows.append(row)
    rows.sort(key=lambda item: (-int(item.get("member_count") or 0), str(item.get("step_class_id") or "")))
    return rows


def statistics_mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(sum(values)) / float(len(values))


def _ratio_dict(
    block_kind_wall: Mapping[str, Sequence[float]],
    wall_values: Sequence[float],
) -> dict[str, float]:
    layer_total = sum(wall_values)
    if layer_total <= 0:
        return {}
    out: dict[str, float] = {}
    for kind, values in block_kind_wall.items():
        out[kind] = round(sum(values) / layer_total, 6)
    return out


def evidence_index_rows(step_rows: Sequence[Mapping[str, Any]], layer_rows: Sequence[Mapping[str, Any]], bubble_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in step_rows:
        for evidence_id in row.get("evidence_ids") or []:
            rows.append(
                {
                    "evidence_id": evidence_id,
                    "kind": "step_window",
                    "rank_id": row.get("rank_id"),
                    "segment_id": row.get("segment_id"),
                    "row_start": row.get("row_start"),
                    "row_end": row.get("row_end"),
                    "summary": f"Step window {row.get('segment_id')}",
                }
            )
    for row in layer_rows:
        for evidence_id in row.get("evidence_ids") or []:
            rows.append(
                {
                    "evidence_id": evidence_id,
                    "kind": "layer_window",
                    "rank_id": row.get("rank_id"),
                    "segment_id": row.get("segment_id"),
                    "layer_id": row.get("layer_id"),
                    "row_start": row.get("row_start"),
                    "row_end": row.get("row_end"),
                    "summary": f"Layer window {row.get('layer_id')}",
                }
            )
    for row in bubble_rows:
        rows.append(
            {
                "evidence_id": row.get("evidence_id"),
                "kind": "bubble_window",
                "rank_id": row.get("rank_id"),
                "segment_id": row.get("segment_id"),
                "row_start": (row.get("before_event") or {}).get("row_idx") if isinstance(row.get("before_event"), dict) else None,
                "row_end": (row.get("after_event") or {}).get("row_idx") if isinstance(row.get("after_event"), dict) else None,
                "summary": f"Bubble {row.get('duration_ms')} ms in {row.get('segment_id')}",
            }
        )
    return rows


def summarize_profile(output_dir: Path) -> dict[str, Any]:
    events = load_events(output_dir / "normalized_event_index.jsonl")
    events_by_rank = group_by_rank(events)
    segments = load_step_segments(output_dir / "step_segments.json")
    layers = load_layer_segments(output_dir / "layer_segments.json")
    blocks = load_block_segments(output_dir / "block_segments.json")
    class_signatures = read_json(output_dir / "class_signatures.json", default={}) or {}
    step_class_by_id: Mapping[str, str] = class_signatures.get("step_class_by_id") or {}
    layer_class_by_id: Mapping[str, str] = class_signatures.get("layer_class_by_id") or {}
    block_class_by_id: Mapping[str, str] = class_signatures.get("block_class_by_id") or {}
    step_classes_meta: Mapping[str, Mapping[str, Any]] = class_signatures.get("step_classes") or {}
    layer_classes_meta: Mapping[str, Mapping[str, Any]] = class_signatures.get("layer_classes") or {}
    block_classes_meta: Mapping[str, Mapping[str, Any]] = class_signatures.get("block_classes") or {}

    rank_rows = rank_summary_rows(events_by_rank, segments)
    step_rows = step_summary_rows(events_by_rank, segments)
    for step in step_rows:
        step["step_class_id"] = step_class_by_id.get(str(step.get("segment_id")))
    row_indexes = row_indexes_by_rank(events_by_rank)
    anatomy_rows = step_anatomy_rows(step_rows, events_by_rank, layers, row_indexes)
    attach_anatomy_to_step_rows(step_rows, anatomy_rows)
    layer_rows = layer_summary_rows(events_by_rank, layers)
    layer_companion_by_id: dict[str, bool] = {}
    layer_block_kinds_by_id: dict[str, list[str]] = defaultdict(list)
    for block in blocks:
        layer_companion_by_id[block.layer_id] = layer_companion_by_id.get(block.layer_id, False) or block.companion_layer
        layer_block_kinds_by_id[block.layer_id].append(block.block_kind)
    for layer in layer_rows:
        layer_id = str(layer.get("layer_id"))
        layer["layer_class_id"] = layer_class_by_id.get(layer_id)
        layer["companion_layer"] = bool(layer_companion_by_id.get(layer_id, False))
        layer["block_kinds"] = layer_block_kinds_by_id.get(layer_id, [])
    block_rows = block_summary_rows(events_by_rank, blocks, block_class_by_id)
    block_class_rows = block_class_summary_rows(block_rows, block_classes_meta)
    layer_class_rows = layer_class_summary_rows(layer_rows, layer_classes_meta, block_rows)
    step_class_rows = step_class_summary_rows(
        step_rows, anatomy_rows, step_classes_meta, layer_rows, block_rows, layer_class_by_id
    )
    operator_rows = operator_summary_rows(events)
    operator_class_rows = operator_class_summary_rows(operator_rows)
    hccl_rows = hccl_op_summary_rows(operator_rows)
    hccl_class_rows = hccl_class_summary_rows(hccl_rows)
    wait_rows = wait_anchor_rows(operator_rows)
    aicpu = aicpu_rows(events_by_rank)
    bubbles = bubble_evidence_rows(step_rows)
    evidence = evidence_index_rows(step_rows, layer_rows, bubbles)
    pipeline_event_count = sum(1 for event in events if has_pipeline_signal(event.pipeline_us))
    pipeline_coverage = round(pipeline_event_count / len(events), 6) if events else 0.0
    operator_pipeline_rows = sum(1 for row in operator_rows if row.get("pipeline_signal"))
    raw_kernel_index = [
        {
            "event_id": event.event_id,
            "rank_id": event.rank_id,
            "source_id": event.source_id,
            "row_idx": event.row_idx,
            "name": event.name_raw,
            "task_type": event.task_type,
            "start_us": event.start_us,
            "duration_us": event.duration_us,
            "wait_us": event.wait_us,
            "roles": list(event.op_roles),
            "categories": list(event.op_categories),
            "shape_signature": event.shape_signature,
        }
        for event in events
    ]
    write_csv(output_dir / "rank_summary.csv", rank_rows)
    write_csv(output_dir / "step_summary.csv", step_rows)
    write_csv(output_dir / "step_anatomy.csv", anatomy_rows)
    write_csv(output_dir / "layer_summary.csv", layer_rows)
    write_csv(output_dir / "block_summary.csv", block_rows)
    write_csv(output_dir / "block_class_summary.csv", block_class_rows)
    write_csv(output_dir / "layer_class_summary.csv", layer_class_rows)
    write_csv(output_dir / "step_class_summary.csv", step_class_rows)
    write_csv(output_dir / "operator_summary.csv", operator_rows)
    write_csv(output_dir / "operator_class_summary.csv", operator_class_rows)
    write_csv(output_dir / "hccl_op_summary.csv", hccl_rows)
    write_csv(output_dir / "hccl_class_summary.csv", hccl_class_rows)
    write_csv(output_dir / "wait_anchor_ops.csv", wait_rows)
    write_csv(output_dir / "aicpu_summary.csv", aicpu)
    write_jsonl(output_dir / "evidence" / "bubble_windows.jsonl", bubbles)
    write_csv(output_dir / "evidence_index.csv", evidence)
    write_csv(output_dir / "raw_kernel_index.csv", raw_kernel_index)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "summarize",
        "created_at": utc_now(),
        "output_dir": str(output_dir),
        "files": {
            "rank_summary": "rank_summary.csv",
            "step_summary": "step_summary.csv",
            "step_anatomy": "step_anatomy.csv",
            "layer_summary": "layer_summary.csv",
            "block_summary": "block_summary.csv",
            "block_class_summary": "block_class_summary.csv",
            "layer_class_summary": "layer_class_summary.csv",
            "step_class_summary": "step_class_summary.csv",
            "operator_summary": "operator_summary.csv",
            "operator_class_summary": "operator_class_summary.csv",
            "hccl_op_summary": "hccl_op_summary.csv",
            "hccl_class_summary": "hccl_class_summary.csv",
            "wait_anchor_ops": "wait_anchor_ops.csv",
            "aicpu_summary": "aicpu_summary.csv",
            "bubble_windows": "evidence/bubble_windows.jsonl",
            "evidence_index": "evidence_index.csv",
            "raw_kernel_index": "raw_kernel_index.csv",
        },
        "counts": {
            "rank_summary_rows": len(rank_rows),
            "step_summary_rows": len(step_rows),
            "step_anatomy_rows": len(anatomy_rows),
            "layer_summary_rows": len(layer_rows),
            "block_summary_rows": len(block_rows),
            "block_class_rows": len(block_class_rows),
            "layer_class_rows": len(layer_class_rows),
            "step_class_rows": len(step_class_rows),
            "operator_summary_rows": len(operator_rows),
            "operator_class_summary_rows": len(operator_class_rows),
            "hccl_op_summary_rows": len(hccl_rows),
            "hccl_class_summary_rows": len(hccl_class_rows),
            "bubble_rows": len(bubbles),
            "wait_anchor_rows": len(wait_rows),
            "aicpu_rows": len(aicpu),
            "evidence_rows": len(evidence),
            "raw_kernel_rows": len(raw_kernel_index),
        },
        "pipeline_coverage": {
            "events_with_pipeline_signal": pipeline_event_count,
            "events_total": len(events),
            "events_ratio": pipeline_coverage,
            "operators_with_pipeline_signal": operator_pipeline_rows,
            "operators_total": len(operator_rows),
        },
        "pipeline_fields": list(PIPELINE_FIELDS),
        "class_summary_counts": {
            "step_classes": len(step_class_rows),
            "layer_classes": len(layer_class_rows),
            "block_classes": len(block_class_rows),
        },
    }
    write_json(output_dir / "summary_manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = summarize_profile(Path(args.output))
    emit_stage_json({"stage": "summarize", "counts": manifest["counts"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
