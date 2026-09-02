#!/usr/bin/env python3
"""Event-set analytics and normalized-artifact loaders.

Interval-union / bubble-window math over ``NormalizedEvent`` sequences,
plus the readers that materialize stage artifacts (normalized events,
step / layer / block segments) back into dataclasses.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from .models import (
        BlockSegment,
        BusySegment,
        LayerSegment,
        NormalizedEvent,
        SourceRef,
        StepSegment,
    )
    from .store import iter_csv_rows, read_json, read_jsonl
except ImportError:  # pragma: no cover - script-mode fallback
    from models import (  # type: ignore[no-redef]
        BlockSegment,
        BusySegment,
        LayerSegment,
        NormalizedEvent,
        SourceRef,
        StepSegment,
    )
    from store import iter_csv_rows, read_json, read_jsonl  # type: ignore[no-redef]


def row_ranges(values: Iterable[int]) -> list[list[int]]:
    ordered = sorted(set(int(value) for value in values))
    if not ordered:
        return []
    ranges: list[list[int]] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append([start, prev])
        start = prev = value
    ranges.append([start, prev])
    return ranges


def quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    left = math.floor(pos)
    right = math.ceil(pos)
    if left == right:
        return float(ordered[left])
    weight = pos - left
    return float(ordered[left] * (1 - weight) + ordered[right] * weight)


def merge_event_segments(events: Sequence[NormalizedEvent]) -> list[BusySegment]:
    ordered = sorted(
        (event for event in events if event.end_us > event.start_us),
        key=lambda item: (item.start_us, item.end_us, item.row_idx),
    )
    if not ordered:
        return []
    segments: list[BusySegment] = []
    start = ordered[0].start_us
    end = ordered[0].end_us
    first_event = ordered[0]
    last_event = ordered[0]
    for event in ordered[1:]:
        if event.start_us <= end:
            if event.end_us > end or (math.isclose(event.end_us, end) and event.row_idx > last_event.row_idx):
                end = max(end, event.end_us)
                last_event = event
            continue
        segments.append(BusySegment(start, end, first_event, last_event))
        start = event.start_us
        end = event.end_us
        first_event = event
        last_event = event
    segments.append(BusySegment(start, end, first_event, last_event))
    return segments


def evidence_event(event: NormalizedEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "event_id": event.event_id,
        "rank_id": event.rank_id,
        "row_idx": event.row_idx,
        "name": event.name_raw,
        "task_type": event.task_type,
        "accelerator_core": event.accelerator_core,
        "stream_id": event.stream_id,
        "start_us": round(event.start_us, 3),
        "duration_us": round(event.duration_us, 3),
        "wait_us": round(event.wait_us, 3),
        "categories": list(event.op_categories),
        "roles": list(event.op_roles),
        "shape_signature": event.shape_signature,
    }


def bubble_windows(events: Sequence[NormalizedEvent], *, limit: int | None = None) -> list[dict[str, Any]]:
    if limit is not None and limit <= 0:
        return []
    segments = merge_event_segments(events)
    if len(segments) < 2:
        return []
    rows: list[dict[str, Any]] = []
    for idx, (left, right) in enumerate(zip(segments[:-1], segments[1:])):
        if right.start_us <= left.end_us:
            continue
        rows.append(
            {
                "bubble_index": idx,
                "start_us": round(left.end_us, 3),
                "end_us": round(right.start_us, 3),
                "duration_us": round(right.start_us - left.end_us, 3),
                "duration_ms": round((right.start_us - left.end_us) / 1000.0, 6),
                "before_event": evidence_event(left.last_event),
                "after_event": evidence_event(right.first_event),
            }
        )
    rows.sort(key=lambda item: float(item["duration_us"]), reverse=True)
    return rows if limit is None else rows[:limit]


def metrics_for_events(events: Sequence[NormalizedEvent], *, top_gap_limit: int = 5) -> dict[str, Any]:
    if not events:
        return {
            "event_count": 0,
            "row_start": None,
            "row_end": None,
            "start_us": None,
            "end_us": None,
            "wall_ms": 0.0,
            "busy_union_ms": 0.0,
            "kernel_sum_ms": 0.0,
            "total_cost_ms": 0.0,
            "wait_sum_ms": 0.0,
            "underfeed_ms": 0.0,
            "underfeed_ratio": 0.0,
            "internal_bubble_total_ms": 0.0,
            "largest_internal_bubble_ms": 0.0,
            "bubble_count": 0,
            "stream_count": 0,
            "task_type_counts": {},
            "role_counts": {},
            "category_counts": {},
            "top_bubbles": [],
        }
    start = min(event.start_us for event in events)
    end = max(event.end_us for event in events)
    wall_us = max(0.0, end - start)
    segments = merge_event_segments(events)
    busy_us = sum(segment.duration_us for segment in segments)
    gaps = [
        right.start_us - left.end_us
        for left, right in zip(segments[:-1], segments[1:])
        if right.start_us > left.end_us
    ]
    kernel_sum_us = sum(event.duration_us for event in events)
    wait_sum_us = sum(event.wait_us for event in events)
    return {
        "event_count": len(events),
        "row_start": min(event.row_idx for event in events),
        "row_end": max(event.row_idx for event in events),
        "start_us": round(start, 3),
        "end_us": round(end, 3),
        "wall_ms": round(wall_us / 1000.0, 6),
        "busy_union_ms": round(busy_us / 1000.0, 6),
        "kernel_sum_ms": round(kernel_sum_us / 1000.0, 6),
        "total_cost_ms": round((kernel_sum_us + wait_sum_us) / 1000.0, 6),
        "wait_sum_ms": round(wait_sum_us / 1000.0, 6),
        "underfeed_ms": round(max(0.0, wall_us - busy_us) / 1000.0, 6),
        "underfeed_ratio": round((max(0.0, wall_us - busy_us) / wall_us) if wall_us > 0 else 0.0, 6),
        "internal_bubble_total_ms": round(sum(gaps) / 1000.0, 6),
        "largest_internal_bubble_ms": round((max(gaps) if gaps else 0.0) / 1000.0, 6),
        "bubble_count": len(gaps),
        "stream_count": len({event.stream_id for event in events}),
        "task_type_counts": dict(sorted(Counter(event.task_type for event in events).items())),
        "role_counts": dict(sorted(Counter(role for event in events for role in event.op_roles).items())),
        "category_counts": dict(sorted(Counter(cat for event in events for cat in event.op_categories).items())),
        "top_bubbles": bubble_windows(events, limit=top_gap_limit) if top_gap_limit > 0 else [],
    }


def select_events(events: Sequence[NormalizedEvent], row_start: int, row_end: int) -> list[NormalizedEvent]:
    left = int(row_start)
    right = int(row_end)
    return [event for event in events if left <= event.row_idx <= right]


def load_events(path: Path) -> list[NormalizedEvent]:
    if path.suffix.lower() == ".csv":
        return load_events_csv(path)
    if not path.exists() and path.with_suffix(".csv").exists():
        return load_events_csv(path.with_suffix(".csv"))
    rows: list[NormalizedEvent] = []
    for item in read_jsonl(path):
        raw_ref = item.get("raw_fields_ref")
        rows.append(
            NormalizedEvent(
                event_id=str(item["event_id"]),
                profile_id=str(item.get("profile_id") or ""),
                rank_id=str(item["rank_id"]),
                source_id=str(item["source_id"]),
                row_idx=int(item["row_idx"]),
                name_raw=str(item.get("name_raw") or ""),
                task_type=str(item.get("task_type") or ""),
                accelerator_core=str(item.get("accelerator_core") or ""),
                stream_id=str(item.get("stream_id") or ""),
                start_us=float(item.get("start_us") or 0.0),
                end_us=float(item.get("end_us") or 0.0),
                duration_us=float(item.get("duration_us") or 0.0),
                wait_us=float(item.get("wait_us") or 0.0),
                op_categories=tuple(item.get("op_categories") or ()),
                op_roles=tuple(item.get("op_roles") or ()),
                shape_signature=item.get("shape_signature"),
                shape_features=dict(item.get("shape_features") or {}),
                pipeline_us=dict(item.get("pipeline_us") or {}),
                op_type=str(item.get("op_type") or "unknown"),
                raw_fields_ref=SourceRef(**raw_ref) if isinstance(raw_ref, dict) else None,
            )
        )
    return sorted(rows, key=lambda event: (event.rank_id, event.row_idx))


def load_events_csv(path: Path) -> list[NormalizedEvent]:
    rows: list[NormalizedEvent] = []
    json_cache: dict[str, Any] = {"[]": [], "{}": {}}
    for _row_number, item in iter_csv_rows(path):
        categories_text = item.get("op_categories") or "[]"
        roles_text = item.get("op_roles") or "[]"
        shape_text = item.get("shape_features") or "{}"
        pipeline_text = item.get("pipeline_us") or "{}"
        categories = json_cache.get(categories_text)
        if categories is None:
            categories = json.loads(categories_text)
            json_cache[categories_text] = categories
        roles = json_cache.get(roles_text)
        if roles is None:
            roles = json.loads(roles_text)
            json_cache[roles_text] = roles
        shape_features = json_cache.get(shape_text)
        if shape_features is None:
            shape_features = json.loads(shape_text)
            json_cache[shape_text] = shape_features
        pipeline_us = json_cache.get(pipeline_text)
        if pipeline_us is None:
            pipeline_us = json.loads(pipeline_text)
            json_cache[pipeline_text] = pipeline_us
        rows.append(
            NormalizedEvent(
                event_id=str(item["event_id"]),
                profile_id=str(item.get("profile_id") or ""),
                rank_id=str(item["rank_id"]),
                source_id=str(item["source_id"]),
                row_idx=int(item["row_idx"]),
                name_raw=str(item.get("name_raw") or ""),
                task_type=str(item.get("task_type") or ""),
                accelerator_core=str(item.get("accelerator_core") or ""),
                stream_id=str(item.get("stream_id") or ""),
                start_us=float(item.get("start_us") or 0.0),
                end_us=float(item.get("end_us") or 0.0),
                duration_us=float(item.get("duration_us") or 0.0),
                wait_us=float(item.get("wait_us") or 0.0),
                op_categories=tuple(categories),
                op_roles=tuple(roles),
                shape_signature=item.get("shape_signature") or None,
                shape_features=dict(shape_features),
                pipeline_us=dict(pipeline_us),
                op_type=str(item.get("op_type") or "unknown"),
                raw_fields_ref=None,
            )
        )
    return rows


def group_by_rank(events: Sequence[NormalizedEvent]) -> dict[str, list[NormalizedEvent]]:
    grouped: dict[str, list[NormalizedEvent]] = {}
    for event in events:
        grouped.setdefault(event.rank_id, []).append(event)
    for rank_events in grouped.values():
        rank_events.sort(key=lambda event: event.row_idx)
    return dict(sorted(grouped.items()))


def load_step_segments(path: Path) -> list[StepSegment]:
    payload = read_json(path, default={})
    rows = payload.get("step_segments", payload if isinstance(payload, list) else [])
    return [
        StepSegment(
            segment_id=str(item["segment_id"]),
            rank_id=str(item["rank_id"]),
            segment_type=str(item["segment_type"]),
            complete=bool(item.get("complete")),
            row_start=int(item["row_start"]),
            row_end=int(item["row_end"]),
            start_us=float(item.get("start_us") or 0.0),
            end_us=float(item.get("end_us") or 0.0),
            cluster_id=item.get("cluster_id"),
            step_family=item.get("step_family"),
            main_layer_count=item.get("main_layer_count"),
            speculative_layer_count=item.get("speculative_layer_count"),
            structure_signature=item.get("structure_signature"),
            layer_ids=tuple(item.get("layer_ids") or ()),
            evidence_ids=tuple(item.get("evidence_ids") or ()),
        )
        for item in rows
    ]


def load_layer_segments(path: Path) -> list[LayerSegment]:
    payload = read_json(path, default={})
    rows = payload.get("layer_segments", payload if isinstance(payload, list) else [])
    return [
        LayerSegment(
            layer_id=str(item["layer_id"]),
            rank_id=str(item["rank_id"]),
            segment_id=str(item["segment_id"]),
            layer_index=int(item["layer_index"]),
            layer_role=str(item.get("layer_role") or "main"),
            boundary_source=str(item.get("boundary_source") or "unknown"),
            row_start=int(item["row_start"]),
            row_end=int(item["row_end"]),
            start_us=float(item.get("start_us") or 0.0),
            end_us=float(item.get("end_us") or 0.0),
            structure_signature=item.get("structure_signature"),
            evidence_ids=tuple(item.get("evidence_ids") or ()),
        )
        for item in rows
    ]


def load_block_segments(path: Path) -> list[BlockSegment]:
    payload = read_json(path, default={})
    rows = payload.get("block_segments", payload if isinstance(payload, list) else [])
    return [
        BlockSegment(
            block_id=str(item["block_id"]),
            rank_id=str(item["rank_id"]),
            segment_id=str(item["segment_id"]),
            layer_id=str(item["layer_id"]),
            layer_index=int(item.get("layer_index") or 0),
            block_index=int(item.get("block_index") or 0),
            block_kind=str(item.get("block_kind") or "other"),
            companion_layer=bool(item.get("companion_layer")),
            row_start=int(item["row_start"]),
            row_end=int(item["row_end"]),
            start_us=float(item.get("start_us") or 0.0),
            end_us=float(item.get("end_us") or 0.0),
            event_count=int(item.get("event_count") or 0),
            evidence_ids=tuple(item.get("evidence_ids") or ()),
        )
        for item in rows
    ]
