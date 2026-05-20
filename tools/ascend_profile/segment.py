#!/usr/bin/env python3
"""Build rank-local step/layer segments from normalized events.

The segmenter is intentionally deterministic.  It does not use duration
percentiles, fuzzy similarity, expected model layer counts, or "best score"
candidate selection.  A step is produced only from exact structural evidence:

1. normalized kernel roles form layer observations;
2. selection/sample kernels and exact recurring templates divide layer streams;
3. full steps are an exact cover over those observations;
4. unexplained middle content is a hard error, not a silent fallback.
"""

from __future__ import annotations

import argparse
import bisect
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    from .common import (
        EvidenceRef,
        LayerSegment,
        NormalizedEvent,
        SCHEMA_VERSION,
        StepSegment,
        StructureObservation,
        TOOL_VERSION,
        group_by_rank,
        load_events,
        metrics_for_events,
        stable_id,
        utc_now,
        write_json,
    )
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import (  # type: ignore[no-redef]
        EvidenceRef,
        LayerSegment,
        NormalizedEvent,
        SCHEMA_VERSION,
        StepSegment,
        StructureObservation,
        TOOL_VERSION,
        group_by_rank,
        load_events,
        metrics_for_events,
        stable_id,
        utc_now,
        write_json,
    )


def event_role(event: NormalizedEvent, role: str) -> bool:
    # High-level roles must come from normalization, not category prefixes.
    # For example attention.csa.compressor is attention evidence, but it is not
    # a primary attention layer anchor.
    return role in event.op_roles


def normalized_name_key(name: str) -> str:
    text = name.lower()
    text = re.sub(r"0x[0-9a-f]+", "", text)
    text = re.sub(r"[0-9a-f]{16,}", "", text)
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"[^a-z_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:96] or "unknown"


def category_family(category: str) -> str:
    if category.startswith("attention.csa."):
        return category
    if category.startswith("attention."):
        return category
    if category.startswith("moe."):
        return category
    if category.startswith("block_head"):
        return "block_head"
    if category == "normalization":
        return "normalization"
    if category == "compute.matmul":
        return "compute.matmul"
    if category == "communication.collective":
        return "communication.collective"
    return category


def primary_attention_category(event: NormalizedEvent) -> str | None:
    matches = [category for category in event.op_categories if category.startswith("attention.") and ".metadata" not in category]
    return sorted(matches)[0] if matches else None


def primary_moe_category(event: NormalizedEvent) -> str | None:
    matches = [category for category in event.op_categories if category.startswith("moe.")]
    return sorted(matches)[0] if matches else None


def anchor_kind(event: NormalizedEvent) -> str | None:
    attention_category = primary_attention_category(event)
    if attention_category is not None and event_role(event, "attention"):
        return attention_category
    moe_category = primary_moe_category(event)
    if moe_category is not None and event_role(event, "moe"):
        return moe_category
    if "compute.matmul" in event.op_categories and event_role(event, "compute"):
        return "compute.matmul"
    if event_role(event, "block_head"):
        return "block_head"
    if event_role(event, "normalization"):
        return "normalization"
    return None


def anchor_signature(event: NormalizedEvent) -> str:
    kind = anchor_kind(event)
    if kind is None:
        categories = ",".join(category_family(item) for item in event.op_categories[:4])
        return categories or "unknown_anchor"
    return f"{kind}:{normalized_name_key(event.name_raw)}"


def event_slice(events: Sequence[NormalizedEvent], row_numbers: Sequence[int], row_start: int, row_end: int) -> list[NormalizedEvent]:
    if row_end < row_start:
        return []
    left = bisect.bisect_left(row_numbers, row_start)
    right = bisect.bisect_right(row_numbers, row_end)
    return list(events[left:right])


def has_row_between(rows: Sequence[int], left_row: int, right_row: int) -> bool:
    pos = bisect.bisect_right(rows, left_row)
    return pos < len(rows) and rows[pos] < right_row


def rows_between(rows: Sequence[int], left_row: int, right_row: int) -> tuple[int, ...]:
    left = bisect.bisect_right(rows, left_row)
    right = bisect.bisect_left(rows, right_row)
    return tuple(rows[left:right])


def latest_row_at_or_before(rows: Sequence[int], row: int, lower_bound: int) -> int | None:
    pos = bisect.bisect_right(rows, row) - 1
    if pos >= 0 and rows[pos] >= lower_bound:
        return int(rows[pos])
    return None


def structural_event(event: NormalizedEvent) -> bool:
    roles = set(event.op_roles)
    return bool(roles & {"attention", "attention_aux", "moe", "block_head", "normalization", "compute"})


def event_identity_key(event: NormalizedEvent) -> tuple[Any, ...]:
    return (
        event.name_raw,
        event.stream_id,
        event.start_us,
        event.end_us,
        event.duration_us,
        event.op_roles,
        event.op_categories,
    )


def dedup_adjacent_event_rows(
    events: Sequence[NormalizedEvent],
    predicate: Any,
) -> tuple[int, ...]:
    """Return evidence rows while ignoring adjacent exact duplicate markers.

    Piecewise traces can duplicate the same visible block-head marker in
    adjacent kernel-detail rows.  The raw duplicate must remain available for
    replay, but it cannot create a second logical layer boundary.
    """

    rows: list[int] = []
    previous_key: tuple[Any, ...] | None = None
    for event in events:
        if not predicate(event):
            previous_key = None
            continue
        key = event_identity_key(event)
        if previous_key == key:
            continue
        rows.append(event.row_idx)
        previous_key = key
    return tuple(rows)


def dedup_adjacent_events(events: Sequence[NormalizedEvent]) -> tuple[NormalizedEvent, ...]:
    deduped: list[NormalizedEvent] = []
    previous_key: tuple[Any, ...] | None = None
    for event in events:
        key = event_identity_key(event)
        if previous_key == key:
            continue
        deduped.append(event)
        previous_key = key
    return tuple(deduped)


def layer_anchor_events(events: Sequence[NormalizedEvent]) -> tuple[NormalizedEvent, ...]:
    """Pick one deterministic layer-anchor family for this rank.

    Attention is the best layer-frequency signal when present.  Dummy/runner
    ranks can miss attention entirely, so the fallback order is MoE, matmul,
    block-head, then normalization.  This is an evidence priority, not a model
    name rule.
    """

    role_order = (
        lambda event: event_role(event, "attention"),
        lambda event: event_role(event, "moe"),
        lambda event: "compute.matmul" in event.op_categories and event_role(event, "compute"),
        lambda event: event_role(event, "block_head"),
        lambda event: event_role(event, "normalization"),
    )
    for predicate in role_order:
        anchors = dedup_adjacent_events(tuple(event for event in events if predicate(event)))
        if anchors:
            return anchors
    return ()


def minimal_exact_period(sequence: Sequence[str]) -> tuple[str, ...]:
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


@dataclass(frozen=True)
class LayerObservation:
    index: int
    row_start: int
    row_end: int
    anchors: tuple[NormalizedEvent, ...]
    signature: str
    regime_key: str


@dataclass(frozen=True)
class LayerFrame:
    layers: tuple[LayerObservation, ...]
    reason: str
    selection_before: tuple[int, ...] = ()
    selection_after: tuple[int, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def row_start(self) -> int:
        return self.layers[0].row_start

    @property
    def row_end(self) -> int:
        return self.layers[-1].row_end

    @property
    def sequence(self) -> tuple[str, ...]:
        return tuple(layer.regime_key for layer in self.layers)

    @property
    def role_sequence(self) -> tuple[str, ...]:
        return tuple(layer_role_key(layer.signature) for layer in self.layers)


def frame_tags(*frames: LayerFrame, extra: Sequence[str] = (), drop: Sequence[str] = ()) -> tuple[str, ...]:
    """Return stable structured tags for control flow.

    ``reason`` is human-readable provenance.  Any logic that needs to know how
    a frame was produced must use these tags instead of parsing reason text.
    """

    dropped = set(drop)
    ordered: dict[str, None] = {}
    for frame in frames:
        for tag in frame.tags:
            if tag in dropped:
                continue
            ordered[tag] = None
    for tag in extra:
        if tag in dropped:
            continue
        ordered[tag] = None
    return tuple(ordered)


def frame_has_tag(frame: LayerFrame, tag: str) -> bool:
    return tag in frame.tags


@dataclass(frozen=True)
class StepPlan:
    frames: tuple[LayerFrame, ...]
    main_frame_count: int
    reason: str
    segment_type: str = "step"
    complete: bool = True

    @property
    def main_layers(self) -> tuple[LayerObservation, ...]:
        layers: list[LayerObservation] = []
        for frame in self.frames[: self.main_frame_count]:
            layers.extend(frame.layers)
        return tuple(layers)

    @property
    def all_layers(self) -> tuple[LayerObservation, ...]:
        layers: list[LayerObservation] = []
        for frame in self.frames:
            layers.extend(frame.layers)
        return tuple(layers)

    @property
    def role_sequence(self) -> tuple[str, ...]:
        return tuple(layer_role_key(layer.signature) for layer in self.main_layers)


def layer_structure_signature(events: Sequence[NormalizedEvent], anchors: Sequence[NormalizedEvent]) -> str:
    categories = Counter(category_family(category) for event in events for category in event.op_categories)
    roles = Counter(role for event in events for role in event.op_roles)
    anchor_terms = tuple(anchor_signature(anchor) for anchor in anchors)
    terms: list[str] = []
    if anchor_terms:
        terms.append("anchors:" + "+".join(anchor_terms))
    attention_terms = sorted(category for category in categories if category.startswith("attention.") and ".metadata" not in category)
    if attention_terms:
        terms.append("attention:" + "+".join(f"{item}x{categories[item]}" for item in attention_terms))
    aux_terms = sorted(category for category in categories if category.startswith("attention.csa."))
    if aux_terms:
        terms.append("attention_aux:" + "+".join(f"{item}x{categories[item]}" for item in aux_terms))
    moe_terms = sorted(category for category in categories if category.startswith("moe."))
    if moe_terms:
        terms.append("moe:" + "+".join(f"{item}x{categories[item]}" for item in moe_terms))
    elif roles.get("compute"):
        terms.append("ffn_or_dense_compute")
    if categories.get("block_head"):
        terms.append("block_head")
    elif roles.get("normalization"):
        terms.append("normalization_no_visible_block_head")
    if roles.get("selection"):
        terms.append("selection")
    if roles.get("communication"):
        terms.append("communication")
    return "|".join(terms) if terms else "no_structural_role"


def layer_regime_key(signature: str) -> str:
    terms = [term for term in signature.split("|") if term not in {"selection", "communication"}]
    key = "|".join(terms) if terms else signature
    key = re.sub(r"block_head(?:x\d+)?", "block_head", key)
    return key


def layer_role_key(signature: str) -> str:
    terms = [
        term
        for term in signature.split("|")
        if term and not term.startswith("anchors:") and term not in {"selection", "communication"}
    ]
    key = "|".join(terms) if terms else signature
    key = key.replace("|selection", "")
    key = re.sub(r"block_head(?:x\d+)?", "block_head", key)
    return key


def group_layer_anchors(anchors: Sequence[NormalizedEvent], boundary_rows: Sequence[int]) -> list[tuple[NormalizedEvent, ...]]:
    """Group same-layer anchors without using timing gaps.

    Multiple attention-like kernels can belong to one layer only when no
    visible block/normalization boundary sits between them.  Otherwise every
    anchor starts a new layer observation.
    """

    groups: list[tuple[NormalizedEvent, ...]] = []
    index = 0
    while index < len(anchors):
        current = anchors[index]
        group = [current]
        index += 1
        while index < len(anchors):
            next_anchor = anchors[index]
            if anchor_kind(next_anchor) != anchor_kind(current):
                break
            if has_row_between(boundary_rows, group[-1].row_idx, next_anchor.row_idx):
                break
            group.append(next_anchor)
            index += 1
        groups.append(tuple(group))
    return groups


def build_layers(events: Sequence[NormalizedEvent], row_numbers: Sequence[int], boundary_rows: Sequence[int], selection_rows: Sequence[int]) -> list[LayerObservation]:
    anchors = layer_anchor_events(events)
    if not anchors:
        return []
    anchor_groups = group_layer_anchors(anchors, boundary_rows)
    starts: list[int] = []
    for index, group in enumerate(anchor_groups):
        lower_bound = events[0].row_idx if index == 0 else anchor_groups[index - 1][-1].row_idx + 1
        selection_pos = bisect.bisect_left(selection_rows, group[0].row_idx)
        if selection_pos > 0 and selection_rows[selection_pos - 1] >= lower_bound:
            lower_bound = selection_rows[selection_pos - 1] + 1
        start = latest_row_at_or_before(boundary_rows, group[0].row_idx, lower_bound)
        starts.append(start if start is not None else group[0].row_idx)

    layers: list[LayerObservation] = []
    for index, group in enumerate(anchor_groups):
        row_start = starts[index]
        row_end = starts[index + 1] - 1 if index + 1 < len(starts) else events[-1].row_idx
        layer_events = event_slice(events, row_numbers, row_start, row_end)
        signature = layer_structure_signature(layer_events, group)
        layers.append(
            LayerObservation(
                index=index,
                row_start=row_start,
                row_end=row_end,
                anchors=tuple(group),
                signature=signature,
                regime_key=layer_regime_key(signature),
            )
        )
    return split_coarse_layers_by_moe(layers, events, row_numbers, boundary_rows, selection_rows)


def split_coarse_layers_by_moe(
    layers: Sequence[LayerObservation],
    events: Sequence[NormalizedEvent],
    row_numbers: Sequence[int],
    boundary_rows: Sequence[int],
    selection_rows: Sequence[int],
) -> list[LayerObservation]:
    """Refine coarse attention observations with MoE anchors.

    If one attention observation encloses multiple MoE anchors, attention is not
    the layer-frequency evidence for that window.  The deterministic repair is
    to split that local window by MoE anchors while preserving the attention
    rows as evidence inside the first refined layer.
    """

    refined: list[LayerObservation] = []
    for layer in layers:
        layer_events = event_slice(events, row_numbers, layer.row_start, layer.row_end)
        moe_anchors = list(dedup_adjacent_events(tuple(event for event in layer_events if event_role(event, "moe") and primary_moe_category(event))))
        if len(moe_anchors) <= 1:
            refined.append(layer)
            continue
        starts: list[int] = []
        for index, anchor in enumerate(moe_anchors):
            if index == 0:
                starts.append(layer.row_start)
                continue
            lower_bound = moe_anchors[index - 1].row_idx + 1
            selection_pos = bisect.bisect_left(selection_rows, anchor.row_idx)
            if selection_pos > 0 and selection_rows[selection_pos - 1] >= lower_bound:
                lower_bound = selection_rows[selection_pos - 1] + 1
            start = latest_row_at_or_before(boundary_rows, anchor.row_idx, lower_bound)
            starts.append(start if start is not None else anchor.row_idx)
        for index, anchor in enumerate(moe_anchors):
            row_start = starts[index]
            row_end = starts[index + 1] - 1 if index + 1 < len(starts) else layer.row_end
            sub_events = event_slice(events, row_numbers, row_start, row_end)
            signature = layer_structure_signature(sub_events, (anchor,))
            refined.append(
                LayerObservation(
                    index=len(refined),
                    row_start=row_start,
                    row_end=row_end,
                    anchors=(anchor,),
                    signature=signature,
                    regime_key=layer_regime_key(signature),
                )
            )
    return [
        LayerObservation(
            index=index,
            row_start=layer.row_start,
            row_end=layer.row_end,
            anchors=layer.anchors,
            signature=layer.signature,
            regime_key=layer.regime_key,
        )
        for index, layer in enumerate(refined)
    ]


def frame_selection_after(frame_layers: Sequence[LayerObservation], next_layer: LayerObservation | None, selection_rows: Sequence[int]) -> tuple[int, ...]:
    if not frame_layers:
        return ()
    left = frame_layers[-1].anchors[-1].row_idx
    right = next_layer.anchors[0].row_idx if next_layer is not None else frame_layers[-1].row_end + 1
    return rows_between(selection_rows, left, right)


def frames_from_selection(layers: Sequence[LayerObservation], selection_rows: Sequence[int]) -> list[LayerFrame]:
    if not layers:
        return []
    frames: list[LayerFrame] = []
    start = 0
    selection_before: tuple[int, ...] = ()
    for index in range(len(layers) - 1):
        between = rows_between(selection_rows, layers[index].anchors[-1].row_idx, layers[index + 1].anchors[0].row_idx)
        if not between:
            continue
        frames.append(
            LayerFrame(
                layers=tuple(layers[start : index + 1]),
                reason="selection_delimited_body",
                selection_before=selection_before,
                selection_after=between,
            )
        )
        start = index + 1
        selection_before = between
    frames.append(
        LayerFrame(
            layers=tuple(layers[start:]),
            reason="selection_delimited_body" if selection_before else "rank_body",
            selection_before=selection_before,
            selection_after=frame_selection_after(layers[start:], None, selection_rows),
        )
    )
    return frames


def sequence_is_exactly_structured(sequence: Sequence[str]) -> bool:
    if len(sequence) <= 1:
        return False
    period = minimal_exact_period(sequence)
    if period and len(period) < len(sequence):
        return True
    counts = Counter(sequence)
    return any(count > 1 for count in counts.values())


def layer_core_set(layers: Sequence[LayerObservation]) -> set[str]:
    return {core_role_key(layer.signature) for layer in layers if core_role_key(layer.signature)}


def layers_have_csa(layers: Sequence[LayerObservation]) -> bool:
    return any("attention:attention.csa" in core_role_key(layer.signature) for layer in layers)


def layers_have_moe(layers: Sequence[LayerObservation]) -> bool:
    return any("moe:" in core_role_key(layer.signature) for layer in layers)


def regime_cut_is_prefix_variant(frame: LayerFrame, cut: int) -> bool:
    """Keep model-local prefix variants inside one body.

    Some models expose early layers without auxiliary attention kernels such as
    CSA compressor/indexer, while the remaining layers include them.  This is a
    layer-family variation, not a workload boundary.  Dense VL+LLM boundaries
    are not suppressed here because they lack the MoE/CSA structural marker.
    """

    left = frame.layers[:cut]
    right = frame.layers[cut:]
    left_core = layer_core_set(left)
    right_core = layer_core_set(right)
    if not left_core or not right_core:
        return False
    same_core_family = bool(left_core & right_core) or left_core.issubset(right_core) or right_core.issubset(left_core)
    if not same_core_family:
        return False
    return (layers_have_csa(left) and layers_have_csa(right)) or (layers_have_moe(left) and layers_have_moe(right))


def split_frame_by_regime(frame: LayerFrame) -> list[LayerFrame]:
    """Split obvious concatenated workloads such as VL body + LLM body.

    This uses exact sequence structure only.  A cut is legal when both sides are
    internally structured and their layer-regime sets are disjoint.  That keeps
    first-layer special cases inside the same model body instead of cutting L0
    away from the recurring suffix.
    """

    sequence = frame.sequence
    if len(sequence) <= 1:
        return [frame]
    cuts = [
        index
        for index in range(1, len(sequence))
        if sequence_is_exactly_structured(sequence[:index])
        and sequence_is_exactly_structured(sequence[index:])
        and set(sequence[:index]).isdisjoint(set(sequence[index:]))
        and not regime_cut_is_prefix_variant(frame, index)
    ]
    if not cuts:
        return [frame]
    frames: list[LayerFrame] = []
    start = 0
    selection_before = frame.selection_before
    for cut in cuts:
        window = tuple(frame.layers[start:cut])
        frames.append(
            LayerFrame(
                layers=window,
                reason=f"{frame.reason}+exact_regime_split",
                selection_before=selection_before,
                selection_after=(),
                tags=frame_tags(frame, extra=("exact_regime_split",)),
            )
        )
        start = cut
        selection_before = ()
    frames.append(
        LayerFrame(
            layers=tuple(frame.layers[start:]),
            reason=f"{frame.reason}+exact_regime_split",
            selection_before=selection_before,
            selection_after=frame.selection_after,
            tags=frame_tags(frame, extra=("exact_regime_split",)),
        )
    )
    return [item for item in frames if item.layers]


def repeated_prefix_end(sequence: Sequence[str], start: int) -> int:
    remaining = len(sequence) - start
    best_end = start
    for width in range(1, remaining + 1):
        unit = tuple(sequence[start : start + width])
        end = start + width
        repeat_count = 1
        while end + width <= len(sequence) and tuple(sequence[end : end + width]) == unit:
            end += width
            repeat_count += 1
        if repeat_count <= 1:
            continue
        if end > best_end:
            best_end = end
    return best_end


def split_frame_by_repeated_body_runs(frame: LayerFrame) -> list[LayerFrame]:
    """Split A/B/A style concatenations without using layer-count constants."""

    if len(frame.layers) <= 2:
        return [frame]
    if not frame_has_primary_attention(frame):
        return [frame]
    sequence = frame_boundary_sequence(frame)
    offsets: list[tuple[int, int]] = []
    start = 0
    while start < len(sequence):
        end = repeated_prefix_end(sequence, start)
        if end <= start:
            return [frame]
        offsets.append((start, end))
        start = end
    if len(offsets) < 3:
        return [frame]
    run_sequences = [sequence[start:end] for start, end in offsets]
    repeated_run = any(
        left_index != right_index and run_sequences[left_index] == run_sequences[right_index]
        for left_index in range(len(run_sequences))
        for right_index in range(left_index + 1, len(run_sequences))
    )
    if not repeated_run:
        return [frame]

    pieces: list[LayerFrame] = []
    for offset, (start, end) in enumerate(offsets):
        pieces.append(
            LayerFrame(
                layers=tuple(frame.layers[start:end]),
                reason=f"{frame.reason}+repeated_body_run_split",
                selection_before=frame.selection_before if offset == 0 else (),
                selection_after=frame.selection_after if offset == len(offsets) - 1 else (),
                tags=frame.tags,
            )
        )
    return pieces


def frame_unit_has_multiple_moe(unit: Sequence[LayerObservation], events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> bool:
    unit_events = event_slice(events, row_numbers, unit[0].row_start, unit[-1].row_end)
    moe_anchor_count = sum(1 for event in unit_events if event_role(event, "moe") and primary_moe_category(event))
    return moe_anchor_count > 1


def frame_unit_has_moe(unit: Sequence[LayerObservation], events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> bool:
    unit_events = event_slice(events, row_numbers, unit[0].row_start, unit[-1].row_end)
    return any(event_role(event, "moe") and primary_moe_category(event) for event in unit_events)


def frame_unit_moe_gating_count(unit: Sequence[LayerObservation], events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> int:
    unit_events = event_slice(events, row_numbers, unit[0].row_start, unit[-1].row_end)
    return sum(1 for event in unit_events if "moe.gating" in event.op_categories)


def layer_has_moe_category(
    layer: LayerObservation,
    events: Sequence[NormalizedEvent],
    row_numbers: Sequence[int],
    category: str,
) -> bool:
    layer_events = event_slice(events, row_numbers, layer.row_start, layer.row_end)
    return any(category in event.op_categories for event in layer_events)


def layer_has_moe_dispatch(
    layer: LayerObservation,
    events: Sequence[NormalizedEvent],
    row_numbers: Sequence[int],
) -> bool:
    layer_events = event_slice(events, row_numbers, layer.row_start, layer.row_end)
    return any(
        category in event.op_categories
        for event in layer_events
        for category in ("moe.dispatch_expert_compute", "moe.dispatch", "moe.combine")
    )


def layer_has_primary_attention(layer: LayerObservation) -> bool:
    # Anchors can be MoE/phase markers after local merges; the layer signature
    # is the stable structural observation for deciding whether the layer body
    # contains a primary attention block.
    return any(term.startswith("attention:") for term in core_role_key(layer.signature).split("|"))


def frame_has_primary_attention(frame: LayerFrame) -> bool:
    return any(layer_has_primary_attention(layer) for layer in frame.layers)


def merge_moe_phase_groups(
    frame: LayerFrame,
    events: Sequence[NormalizedEvent],
    row_numbers: Sequence[int],
) -> LayerFrame:
    """Merge MoE phases that form exact gating-started groups."""

    if len(frame.layers) < 2:
        return frame
    groups: list[tuple[int, int]] = []
    index = 0
    changed = False
    while index < len(frame.layers):
        first = frame.layers[index]
        if not layer_has_moe_category(first, events, row_numbers, "moe.gating"):
            return frame
        cursor = index + 1
        while cursor < len(frame.layers):
            current = frame.layers[cursor]
            if layer_has_moe_category(current, events, row_numbers, "moe.gating"):
                break
            if not layer_has_moe_dispatch(current, events, row_numbers):
                return frame
            cursor += 1
        if cursor == index + 1 and not layer_has_moe_dispatch(first, events, row_numbers):
            return frame
        if cursor > index + 1:
            changed = True
        groups.append((index, cursor))
        index = cursor
    if not changed:
        return frame

    merged_layers: list[LayerObservation] = []
    for start, end in groups:
        window = frame.layers[start:end]
        anchors: list[NormalizedEvent] = []
        for layer in window:
            anchors.extend(layer.anchors)
        row_start = window[0].row_start
        row_end = window[-1].row_end
        layer_events = event_slice(events, row_numbers, row_start, row_end)
        signature = layer_structure_signature(layer_events, anchors)
        merged_layers.append(
            LayerObservation(
                index=len(merged_layers),
                row_start=row_start,
                row_end=row_end,
                anchors=tuple(anchors),
                signature=signature,
                regime_key=layer_regime_key(signature),
            )
        )
    return LayerFrame(
        layers=tuple(merged_layers),
        reason=f"{frame.reason}+exact_moe_phase_group_merge",
        selection_before=frame.selection_before,
        selection_after=frame.selection_after,
        tags=frame.tags,
    )


def merge_adjacent_moe_gating_dispatch_pairs(
    frame: LayerFrame,
    events: Sequence[NormalizedEvent],
    row_numbers: Sequence[int],
) -> LayerFrame:
    """Merge exact MoE two-phase observations into one logical layer.

    Some kernels expose a layer as adjacent observations: routing/gating first,
    then dispatch/combine/expert compute.  This path is allowed only when the
    whole frame is an exact pair cover; otherwise it leaves the frame unchanged.
    """

    if len(frame.layers) < 2 or len(frame.layers) % 2:
        return frame
    merged_layers: list[LayerObservation] = []
    for index in range(0, len(frame.layers), 2):
        left = frame.layers[index]
        right = frame.layers[index + 1]
        if not layer_has_moe_category(left, events, row_numbers, "moe.gating"):
            return frame
        if layer_has_moe_dispatch(left, events, row_numbers):
            return frame
        if layer_has_moe_category(right, events, row_numbers, "moe.gating"):
            return frame
        if not layer_has_moe_dispatch(right, events, row_numbers):
            return frame
        anchors: list[NormalizedEvent] = []
        anchors.extend(left.anchors)
        anchors.extend(right.anchors)
        row_start = left.row_start
        row_end = right.row_end
        layer_events = event_slice(events, row_numbers, row_start, row_end)
        signature = layer_structure_signature(layer_events, anchors)
        merged_layers.append(
            LayerObservation(
                index=len(merged_layers),
                row_start=row_start,
                row_end=row_end,
                anchors=tuple(anchors),
                signature=signature,
                regime_key=layer_regime_key(signature),
            )
        )
    return LayerFrame(
        layers=tuple(merged_layers),
        reason=f"{frame.reason}+exact_moe_gating_dispatch_pair_merge",
        selection_before=frame.selection_before,
        selection_after=frame.selection_after,
        tags=frame.tags,
    )


def layer_primary_attention_categories(layer: LayerObservation) -> tuple[str, ...]:
    return tuple(sorted({category for anchor in layer.anchors if (category := primary_attention_category(anchor))}))


def merge_adjacent_attention_pair_single_moe(
    frame: LayerFrame,
    events: Sequence[NormalizedEvent],
    row_numbers: Sequence[int],
) -> LayerFrame:
    """Merge exact two-attention observations that share one MoE block."""

    if len(frame.layers) < 2 or len(frame.layers) % 2:
        return frame
    merged_layers: list[LayerObservation] = []
    for index in range(0, len(frame.layers), 2):
        left = frame.layers[index]
        right = frame.layers[index + 1]
        left_attention = layer_primary_attention_categories(left)
        right_attention = layer_primary_attention_categories(right)
        if not left_attention or left_attention != right_attention:
            return frame
        if frame_unit_moe_gating_count((left, right), events, row_numbers) != 1:
            return frame
        anchors: list[NormalizedEvent] = []
        anchors.extend(left.anchors)
        anchors.extend(right.anchors)
        row_start = left.row_start
        row_end = right.row_end
        layer_events = event_slice(events, row_numbers, row_start, row_end)
        signature = layer_structure_signature(layer_events, anchors)
        merged_layers.append(
            LayerObservation(
                index=len(merged_layers),
                row_start=row_start,
                row_end=row_end,
                anchors=tuple(anchors),
                signature=signature,
                regime_key=layer_regime_key(signature),
            )
        )
    return LayerFrame(
        layers=tuple(merged_layers),
        reason=f"{frame.reason}+exact_attention_pair_single_moe_merge",
        selection_before=frame.selection_before,
        selection_after=frame.selection_after,
        tags=frame.tags,
    )


def merge_exact_attention_subunits(frame: LayerFrame, events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> LayerFrame:
    """Merge exact attention subunits that compose one transformer layer.

    DeepSeek-style MLA can expose two attention anchors for one logical layer.
    The proof is exact: the frame has a repeated multi-observation period, at
    least one period contains a MoE block, and no period contains more than one
    MoE block.  Hybrid-attention models with one MoE per attention observation
    therefore remain unmerged.
    """

    sequence = tuple("+".join(anchor_signature(anchor) for anchor in layer.anchors) for layer in frame.layers)
    period = minimal_exact_period(sequence)
    if not period or len(period) <= 1 or len(period) >= len(sequence):
        return frame
    width = len(period)
    windows = [frame.layers[index : index + width] for index in range(0, len(frame.layers), width)]
    if any(len(window) != width for window in windows):
        return frame
    if not any(frame_unit_has_moe(window, events, row_numbers) for window in windows):
        return frame
    if any(frame_unit_has_multiple_moe(window, events, row_numbers) for window in windows):
        return frame

    merged_layers: list[LayerObservation] = []
    for window in windows:
        anchors: list[NormalizedEvent] = []
        for layer in window:
            anchors.extend(layer.anchors)
        row_start = window[0].row_start
        row_end = window[-1].row_end
        layer_events = event_slice(events, row_numbers, row_start, row_end)
        signature = layer_structure_signature(layer_events, anchors)
        merged_layers.append(
            LayerObservation(
                index=len(merged_layers),
                row_start=row_start,
                row_end=row_end,
                anchors=tuple(anchors),
                signature=signature,
                regime_key=layer_regime_key(signature),
            )
        )
    return LayerFrame(
        layers=tuple(merged_layers),
        reason=f"{frame.reason}+exact_attention_subunit_merge",
        selection_before=frame.selection_before,
        selection_after=frame.selection_after,
        tags=frame.tags,
    )


def merge_exact_moe_subunits(frame: LayerFrame, events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> LayerFrame:
    sequence = frame.role_sequence
    period = minimal_exact_period(sequence)
    if not period or len(period) <= 1 or len(period) >= len(sequence):
        return frame
    width = len(period)
    windows = [frame.layers[index : index + width] for index in range(0, len(frame.layers), width)]
    if any(len(window) != width for window in windows):
        return frame
    if any(frame_unit_moe_gating_count(window, events, row_numbers) != 1 for window in windows):
        return frame

    merged_layers: list[LayerObservation] = []
    for window in windows:
        anchors: list[NormalizedEvent] = []
        for layer in window:
            anchors.extend(layer.anchors)
        row_start = window[0].row_start
        row_end = window[-1].row_end
        layer_events = event_slice(events, row_numbers, row_start, row_end)
        signature = layer_structure_signature(layer_events, anchors)
        merged_layers.append(
            LayerObservation(
                index=len(merged_layers),
                row_start=row_start,
                row_end=row_end,
                anchors=tuple(anchors),
                signature=signature,
                regime_key=layer_regime_key(signature),
            )
        )
    return LayerFrame(
        layers=tuple(merged_layers),
        reason=f"{frame.reason}+exact_moe_subunit_merge",
        selection_before=frame.selection_before,
        selection_after=frame.selection_after,
        tags=frame.tags,
    )


def frame_has_moe(frame: LayerFrame, events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> bool:
    frame_events = event_slice(events, row_numbers, frame.row_start, frame.row_end)
    return any(event_role(event, "moe") for event in frame_events)


def attention_terms(signature: str) -> tuple[str, ...]:
    terms: list[str] = []
    for term in layer_role_key(signature).split("|"):
        if not term.startswith("attention:"):
            continue
        text = term[len("attention:") :]
        for item in text.split("+"):
            name, _count = split_role_count(item)
            if name:
                terms.append(name)
    return tuple(sorted(set(terms)))


def split_role_count(item: str) -> tuple[str, str]:
    match = re.fullmatch(r"(.+)x(\d+)", item)
    if match:
        return match.group(1), match.group(2)
    return item, ""


def dense_prefix_matches_moe_suffix_attention(left: LayerFrame, right: LayerFrame) -> bool:
    left_attention: set[str] = set()
    for layer in left.layers:
        left_attention.update(attention_terms(layer.signature))
    if not left_attention:
        return False
    for layer in right.layers:
        right_attention = set(attention_terms(layer.signature))
        if not left_attention.issubset(right_attention):
            return False
    return True


def frame_core_roles(frame: LayerFrame) -> set[str]:
    roles: set[str] = set()
    for layer in frame.layers:
        key = core_role_key(layer.signature)
        if key:
            roles.add(key)
    return roles


def layer_merge_key(signature: str) -> str:
    anchor_terms = tuple(term for term in signature.split("|") if term.startswith("anchors:"))
    core = core_role_key(signature)
    return "|".join((*anchor_terms, core))


def frame_merge_roles(frame: LayerFrame) -> set[str]:
    return {key for layer in frame.layers if (key := layer_merge_key(layer.signature))}


def merge_adjacent_same_core_frames(frames: Sequence[LayerFrame]) -> list[LayerFrame]:
    """Merge adjacent no-selection frames that only differ by aux evidence."""

    merged: list[LayerFrame] = []
    index = 0
    while index < len(frames):
        current = frames[index]
        while index + 1 < len(frames):
            nxt = frames[index + 1]
            if current.selection_after or nxt.selection_before:
                break
            current_roles = frame_merge_roles(current)
            next_roles = frame_merge_roles(nxt)
            if not current_roles or current_roles != next_roles:
                break
            current = LayerFrame(
                layers=(*current.layers, *nxt.layers),
                reason=f"{current.reason}+same_core_prefix_variant_merge",
                selection_before=current.selection_before,
                selection_after=nxt.selection_after,
                tags=frame_tags(current, nxt),
            )
            index += 1
        merged.append(current)
        index += 1
    return merged


def split_attention_prefix_moe_suffix(frame: LayerFrame, events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> list[LayerFrame]:
    """Split transitions between attention body and no-attention MoE tail."""

    if len(frame.layers) <= 1:
        return [frame]
    if not any(layer_has_primary_attention(layer) for layer in frame.layers):
        return [frame]

    pieces: list[LayerFrame] = []
    start = 0
    current_has_attention = layer_has_primary_attention(frame.layers[0])
    for index in range(1, len(frame.layers)):
        has_attention = layer_has_primary_attention(frame.layers[index])
        if has_attention == current_has_attention:
            continue
        previous = frame.layers[index - 1]
        current = frame.layers[index]
        previous_has_moe = frame_has_moe(LayerFrame((previous,), reason=frame.reason), events, row_numbers)
        current_has_moe = frame_has_moe(LayerFrame((current,), reason=frame.reason), events, row_numbers)
        if not (previous_has_moe or current_has_moe):
            continue
        pieces.append(
            LayerFrame(
                layers=tuple(frame.layers[start:index]),
                reason=frame.reason if current_has_attention else f"{frame.reason}+no_attention_moe_suffix",
                selection_before=frame.selection_before if not pieces else (),
                selection_after=(),
                tags=frame.tags,
            )
        )
        start = index
        current_has_attention = has_attention
    if not pieces:
        return [frame]
    pieces.append(
        LayerFrame(
            layers=tuple(frame.layers[start:]),
            reason=frame.reason if current_has_attention else f"{frame.reason}+no_attention_moe_suffix",
            selection_before=(),
            selection_after=frame.selection_after,
            tags=frame.tags,
        )
    )
    return pieces


def merge_dense_prefix_with_moe_suffix(frames: Sequence[LayerFrame], events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> list[LayerFrame]:
    merged: list[LayerFrame] = []
    index = 0
    while index < len(frames):
        if index + 1 >= len(frames):
            merged.append(frames[index])
            index += 1
            continue
        left = frames[index]
        right = frames[index + 1]
        if (
            not left.selection_after
            and not right.selection_before
            and not frame_has_moe(left, events, row_numbers)
            and frame_has_moe(right, events, row_numbers)
            and dense_prefix_matches_moe_suffix_attention(left, right)
        ):
            merged.append(
                LayerFrame(
                    layers=(*left.layers, *right.layers),
                    reason=f"{left.reason}+{right.reason}+dense_prefix_moe_suffix",
                    selection_before=left.selection_before,
                    selection_after=right.selection_after,
                    tags=frame_tags(left, right),
                )
            )
            index += 2
            continue
        merged.append(left)
        index += 1
    return merged


def supported_exact_templates_from_sequences(
    sequences: Sequence[tuple[str, ...]],
    *,
    min_count: int = 2,
) -> tuple[tuple[str, ...], ...]:
    counts = Counter(sequence for sequence in sequences if len(sequence) > 1)
    # "Appears more than once" is recurrence evidence, not approximate matching.
    return tuple(sequence for sequence, count in counts.items() if count >= min_count)


def supported_exact_templates(frames: Sequence[LayerFrame]) -> tuple[tuple[str, ...], ...]:
    return supported_exact_templates_from_sequences(tuple(frame.sequence for frame in frames))


def boundary_template_seed_frames(frames: Sequence[LayerFrame]) -> tuple[LayerFrame, ...]:
    return tuple(
        frame
        for frame in frames
        if len(frame.layers) > 1
        and frame_has_primary_attention(frame)
        and frame.selection_after
        and not frame_has_tag(frame, "exact_template_prefix_residual")
    )


def exact_cover_sequence(sequence: Sequence[str], templates: Sequence[tuple[str, ...]]) -> list[tuple[int, int]] | None:
    if len(sequence) <= 1:
        return None
    ordered = sorted({tuple(template) for template in templates if 0 < len(template) < len(sequence)}, key=len, reverse=True)
    if not ordered:
        return None
    values = tuple(sequence)
    memo: dict[int, list[tuple[int, int]] | None] = {}

    def search(index: int) -> list[tuple[int, int]] | None:
        if index == len(values):
            return []
        if index in memo:
            return memo[index]
        for template in ordered:
            end = index + len(template)
            if end > len(values):
                continue
            if values[index:end] != template:
                continue
            suffix = search(end)
            if suffix is not None:
                memo[index] = [(index, end), *suffix]
                return memo[index]
        memo[index] = None
        return None

    path = search(0)
    if path is None or len(path) <= 1:
        return None
    return path


def exact_prefix_cover_sequence(sequence: Sequence[str], templates: Sequence[tuple[str, ...]]) -> list[tuple[int, int]] | None:
    if len(sequence) <= 1:
        return None
    ordered = sorted({tuple(template) for template in templates if 0 < len(template) < len(sequence)}, key=len, reverse=True)
    if not ordered:
        return None
    values = tuple(sequence)
    index = 0
    path: list[tuple[int, int]] = []
    while index < len(values):
        matched: tuple[str, ...] | None = None
        for template in ordered:
            end = index + len(template)
            if end > len(values):
                continue
            if values[index:end] == template:
                matched = template
                break
        if matched is None:
            break
        end = index + len(matched)
        path.append((index, end))
        index = end
    if not path or index >= len(values):
        return None
    return path


def exact_suffix_cover_sequence(sequence: Sequence[str], templates: Sequence[tuple[str, ...]]) -> tuple[int, list[tuple[int, int]]] | None:
    if len(sequence) <= 1:
        return None
    for residual_end in range(1, len(sequence)):
        cover = exact_cover_sequence(sequence[residual_end:], templates)
        if cover is None:
            continue
        shifted = [(start + residual_end, end + residual_end) for start, end in cover]
        if shifted:
            return residual_end, shifted
    return None


def split_frames_by_exact_templates(frames: Sequence[LayerFrame]) -> list[LayerFrame]:
    templates = supported_exact_templates(frames)
    core_templates = supported_exact_templates_from_sequences(tuple(frame_core_sequence(frame) for frame in frames))
    boundary_templates = supported_exact_templates_from_sequences(
        tuple(frame_boundary_sequence(frame) for frame in boundary_template_seed_frames(frames))
    )
    all_templates = supported_exact_templates_from_sequences(tuple(frame.sequence for frame in frames), min_count=1)
    all_core_templates = supported_exact_templates_from_sequences(tuple(frame_core_sequence(frame) for frame in frames), min_count=1)
    if not templates and not core_templates and not boundary_templates and not all_templates and not all_core_templates:
        return list(frames)
    split: list[LayerFrame] = []
    for frame in frames:
        if frame_has_tag(frame, "exact_regime_split"):
            split.append(frame)
            continue
        cover = exact_cover_sequence(frame.sequence, templates)
        if cover is None:
            cover = exact_cover_sequence(frame_core_sequence(frame), core_templates)
        if cover is None:
            cover = exact_cover_sequence(frame.sequence, all_templates)
        if cover is None:
            cover = exact_cover_sequence(frame_core_sequence(frame), all_core_templates)
        residual_start: int | None = None
        if cover is None:
            cover = exact_prefix_cover_sequence(frame_core_sequence(frame), core_templates)
            if cover is not None:
                residual_start = cover[-1][1]
        if cover is None:
            cover = exact_prefix_cover_sequence(frame_boundary_sequence(frame), boundary_templates)
            if cover is not None:
                residual_start = cover[-1][1]
        residual_end: int | None = None
        if cover is None:
            suffix_cover = exact_suffix_cover_sequence(frame_boundary_sequence(frame), boundary_templates)
            if suffix_cover is not None:
                residual_end, cover = suffix_cover
        if cover is None:
            split.append(frame)
            continue
        if residual_end is not None:
            split.append(
                LayerFrame(
                    layers=tuple(frame.layers[:residual_end]),
                    reason=f"{frame.reason}+exact_template_suffix_residual",
                    selection_before=frame.selection_before,
                    selection_after=(),
                    tags=frame_tags(
                        frame,
                        extra=("exact_template_suffix_residual",),
                        drop=("exact_template_cover", "exact_template_prefix_residual"),
                    ),
                )
            )
        for offset, (start, end) in enumerate(cover):
            split.append(
                LayerFrame(
                    layers=tuple(frame.layers[start:end]),
                    reason=f"{frame.reason}+exact_template_cover",
                    selection_before=frame.selection_before if offset == 0 and residual_end is None else (),
                    selection_after=frame.selection_after if offset == len(cover) - 1 and residual_start is None else (),
                    tags=frame_tags(
                        frame,
                        extra=("exact_template_cover",),
                        drop=("exact_template_prefix_residual", "exact_template_suffix_residual"),
                    ),
                )
            )
        if residual_start is not None:
            split.append(
                LayerFrame(
                    layers=tuple(frame.layers[residual_start:]),
                    reason=f"{frame.reason}+exact_template_prefix_residual",
                    selection_before=(),
                    selection_after=frame.selection_after,
                    tags=frame_tags(
                        frame,
                        extra=("exact_template_prefix_residual",),
                        drop=("exact_template_cover", "exact_template_suffix_residual"),
                    ),
                )
            )
    return split


def split_no_attention_frames_by_observed_main_lengths(frames: Sequence[LayerFrame]) -> list[LayerFrame]:
    attention_templates = [
        frame
        for frame in frames
        if frame_has_primary_attention(frame) and len(frame.layers) > 1
    ]
    if not attention_templates:
        return list(frames)
    template_by_length: dict[int, LayerFrame] = {}
    for frame in attention_templates:
        template_by_length.setdefault(len(frame.layers), frame)
    lengths = sorted(template_by_length, reverse=True)

    split: list[LayerFrame] = []
    for frame in frames:
        if frame_has_primary_attention(frame) or len(frame.layers) <= 1:
            split.append(frame)
            continue
        chosen: int | None = None
        for length in lengths:
            if length >= len(frame.layers) or len(frame.layers) % length != 0:
                continue
            chunk = LayerFrame(tuple(frame.layers[:length]), reason=frame.reason, tags=frame.tags)
            chunk_components = frame_coarse_core_components(chunk)
            template_components = frame_coarse_core_components(template_by_length[length])
            if all(template_components[key] >= value for key, value in chunk_components.items()):
                chosen = length
                break
        if chosen is None:
            prefix_split = split_no_attention_prefix_by_observed_main_length(frame, template_by_length, lengths)
            if prefix_split is not None:
                split.extend(prefix_split)
            else:
                split.append(frame)
            continue
        for offset, start in enumerate(range(0, len(frame.layers), chosen)):
            end = start + chosen
            split.append(
                LayerFrame(
                    layers=tuple(frame.layers[start:end]),
                    reason=f"{frame.reason}+no_attention_composite_split_by_observed_main_length",
                    selection_before=frame.selection_before if offset == 0 else (),
                    selection_after=frame.selection_after if end == len(frame.layers) else (),
                    tags=frame.tags,
                )
            )
    return split


def split_no_attention_prefix_by_observed_main_length(
    frame: LayerFrame,
    template_by_length: dict[int, LayerFrame],
    lengths: Sequence[int],
) -> list[LayerFrame] | None:
    """Split no-attention ``main + suffix`` using observed attention bodies."""

    for length in lengths:
        if length >= len(frame.layers):
            continue
        chunks: list[LayerFrame] = []
        start = 0
        while start + length <= len(frame.layers):
            chunk = LayerFrame(tuple(frame.layers[start : start + length]), reason=frame.reason, tags=frame.tags)
            chunk_components = frame_coarse_core_components(chunk)
            template_components = frame_coarse_core_components(template_by_length[length])
            if not all(template_components[key] >= value for key, value in chunk_components.items()):
                break
            chunks.append(chunk)
            start += length
        if not chunks or start >= len(frame.layers):
            continue
        suffix = LayerFrame(tuple(frame.layers[start:]), reason=frame.reason, tags=frame.tags)
        if not strict_core_component_substructure(suffix, chunks[-1]):
            continue
        pieces: list[LayerFrame] = []
        for offset, chunk in enumerate(chunks):
            pieces.append(
                LayerFrame(
                    layers=chunk.layers,
                    reason=f"{frame.reason}+no_attention_prefix_split_by_observed_main_length",
                    selection_before=frame.selection_before if offset == 0 else (),
                    selection_after=(),
                    tags=frame.tags,
                )
            )
        pieces.append(
            LayerFrame(
                layers=suffix.layers,
                reason=f"{frame.reason}+no_attention_suffix_split_by_observed_main_length",
                selection_before=(),
                selection_after=frame.selection_after,
                tags=frame.tags,
            )
        )
        return pieces
    return None


def split_composite_frames(frames: Sequence[LayerFrame], events: Sequence[NormalizedEvent], row_numbers: Sequence[int]) -> list[LayerFrame]:
    current: list[LayerFrame] = []
    for frame in frames:
        merged = merge_exact_attention_subunits(frame, events, row_numbers)
        for split in split_frame_by_regime(merged):
            attention_merged = merge_adjacent_attention_pair_single_moe(split, events, row_numbers)
            moe_merged = merge_exact_moe_subunits(attention_merged, events, row_numbers)
            current.append(merge_moe_phase_groups(moe_merged, events, row_numbers))
    current = [piece for frame in current for piece in split_frame_by_regime(frame)]
    current = [piece for frame in current for piece in split_attention_prefix_moe_suffix(frame, events, row_numbers)]
    current = merge_adjacent_same_core_frames(current)
    current = merge_dense_prefix_with_moe_suffix(current, events, row_numbers)
    while True:
        refined = [piece for frame in current for piece in split_frame_by_regime(frame)]
        refined = [piece for frame in refined for piece in split_frame_by_repeated_body_runs(frame)]
        refined = split_frames_by_exact_templates(refined)
        refined = split_no_attention_frames_by_observed_main_lengths(refined)
        if [(item.row_start, item.row_end, item.sequence) for item in refined] == [
            (item.row_start, item.row_end, item.sequence) for item in current
        ]:
            return refined
        current = refined


def sequence_counter(sequence: Sequence[str]) -> Counter[str]:
    return Counter(sequence)


def strict_substructure(candidate: Sequence[str], reference: Sequence[str]) -> bool:
    if not candidate or not reference:
        return False
    candidate_counter = sequence_counter(candidate)
    reference_counter = sequence_counter(reference)
    if candidate_counter == reference_counter:
        return False
    return all(reference_counter[key] >= value for key, value in candidate_counter.items())


def core_role_key(signature: str) -> str:
    """Return the structural core without auxiliary implementation details."""

    kept_terms: list[str] = []
    for term in layer_role_key(signature).split("|"):
        if not term:
            continue
        role, _, values = term.partition(":")
        if role in {"attention_aux", "aicpu", "communication"}:
            continue
        if role == "attention":
            kept_values: list[str] = []
            for item in values.split("+"):
                category, count = split_role_count(item)
                if category in {"attention.csa.metadata", "attention.csa.indexer", "attention.csa.compressor"}:
                    continue
                if category:
                    kept_values.append(f"{category}x{count}" if count else category)
            if kept_values:
                kept_terms.append(f"{role}:{'+'.join(sorted(kept_values))}")
            continue
        kept_terms.append(term)
    return "|".join(sorted(kept_terms))


def frame_core_sequence(frame: LayerFrame) -> tuple[str, ...]:
    return tuple(core_role_key(layer.signature) for layer in frame.layers)


def layer_boundary_key(signature: str) -> str:
    """Return the stable sequence key used to prove step boundaries.

    The repeated boundary of a transformer-like body is carried primarily by
    the attention/block-head side.  MoE/FFN implementation details can vary
    inside a layer because of EP routing or fusion, so they must not be the only
    reason a proven main-body period stops matching.
    """

    terms = layer_role_key(signature).split("|")
    attention_terms = [term for term in terms if term.startswith("attention:")]
    if attention_terms:
        kept: list[str] = []
        for term in attention_terms:
            role, _, values = term.partition(":")
            categories = sorted({split_role_count(item)[0] for item in values.split("+") if split_role_count(item)[0]})
            kept.append(f"{role}:{'+'.join(categories)}")
        for term in terms:
            if not term.startswith("attention_aux:"):
                continue
            role, _, values = term.partition(":")
            categories = sorted({split_role_count(item)[0] for item in values.split("+") if split_role_count(item)[0]})
            kept.append(f"{role}:{'+'.join(categories)}")
        return "|".join(sorted(kept))
    return core_role_key(signature)


def frame_boundary_sequence(frame: LayerFrame) -> tuple[str, ...]:
    return tuple(layer_boundary_key(layer.signature) for layer in frame.layers)


def frame_attention_stream_sequence(frame: LayerFrame) -> tuple[str, ...]:
    streams: list[str] = []
    for layer in frame.layers:
        attention_streams = tuple(anchor.stream_id for anchor in layer.anchors if primary_attention_category(anchor) is not None)
        streams.append(attention_streams[0] if attention_streams else "")
    return tuple(streams)


def longest_template_slice_at(
    sequence: Sequence[str],
    start: int,
    template: Sequence[str],
) -> tuple[int, int] | None:
    """Find the longest exact template slice beginning at sequence[start]."""

    best: tuple[int, int] | None = None
    values = tuple(sequence)
    unit = tuple(template)
    for template_start in range(len(unit)):
        length = 0
        while start + length < len(values) and template_start + length < len(unit):
            if values[start + length] != unit[template_start + length]:
                break
            length += 1
        if length == 0:
            continue
        if best is None or length > best[1]:
            best = (template_start, length)
    return best


def piecewise_template_slice_cover(
    sequence: Sequence[str],
    stream_sequence: Sequence[str],
    templates: Sequence[tuple[str, ...]],
) -> list[tuple[int, int, int]] | None:
    """Cover an interleaved piecewise body with exact slices of one template.

    Piecewise graph can emit a complete logical step as multiple stream chunks.
    In kernel-details row order this can look like several incomplete template
    slices interleaved together.  This recognizer does not create steps; it only
    proves that the middle window is an explained piecewise interleave instead
    of an unexplained residual.
    """

    values = tuple(sequence)
    streams = tuple(stream_sequence)
    if len(values) <= 1 or len(values) != len(streams):
        return None
    for template in sorted({tuple(item) for item in templates if len(item) > 1}, key=len, reverse=True):
        index = 0
        cover: list[tuple[int, int, int]] = []
        while index < len(values):
            matched = longest_template_slice_at(values, index, template)
            if matched is None:
                break
            template_start, length = matched
            cover.append((index, index + length, template_start))
            index += length
        if index != len(values):
            continue
        template_offsets = {template_start for _start, _end, template_start in cover}
        stream_changes = sum(1 for left, right in zip(streams[:-1], streams[1:]) if left and right and left != right)
        has_interleaved_slice = any(template_start > 0 for _start, _end, template_start in cover)
        if len(cover) > 1 and len(template_offsets) > 1 and stream_changes > 0 and has_interleaved_slice:
            return cover
        if len(cover) == 1 and has_interleaved_slice and stream_changes > 0 and len(values) < len(template):
            return cover
    return None


def core_components(signature: str) -> Counter[str]:
    components: Counter[str] = Counter()
    for term in core_role_key(signature).split("|"):
        if not term:
            continue
        role, _, values = term.partition(":")
        if not values:
            components[role] += 1
            continue
        for item in values.split("+"):
            category, count = split_role_count(item)
            if category:
                components[f"{role}:{category}"] += int(count or "1")
    return components


def frame_core_components(frame: LayerFrame) -> Counter[str]:
    total: Counter[str] = Counter()
    for layer in frame.layers:
        total.update(core_components(layer.signature))
    return total


def coarse_core_components(signature: str) -> Counter[str]:
    components: Counter[str] = Counter()
    for term in core_role_key(signature).split("|"):
        if not term:
            continue
        role, _, values = term.partition(":")
        if not values:
            components[role] += 1
            continue
        components[role] += len([item for item in values.split("+") if item])
    return components


def frame_coarse_core_components(frame: LayerFrame) -> Counter[str]:
    total: Counter[str] = Counter()
    for layer in frame.layers:
        total.update(coarse_core_components(layer.signature))
    return total


def strict_core_component_substructure(candidate: LayerFrame, reference: LayerFrame) -> bool:
    # Speculative/dummy tails can use different kernel implementations for the
    # same high-level block role.  For attachment, require exact role support
    # rather than exact kernel-category support.
    candidate_counter = frame_coarse_core_components(candidate)
    reference_counter = frame_coarse_core_components(reference)
    if not candidate_counter or candidate_counter == reference_counter:
        return False
    return all(reference_counter[key] >= value for key, value in candidate_counter.items())


def strict_boundary_substructure(candidate: LayerFrame, reference: LayerFrame) -> bool:
    candidate_counter = Counter(frame_boundary_sequence(candidate))
    reference_counter = Counter(frame_boundary_sequence(reference))
    if not candidate_counter or candidate_counter == reference_counter:
        return False
    return all(reference_counter[key] >= value for key, value in candidate_counter.items())


def compose_step_plans(frames: Sequence[LayerFrame]) -> list[StepPlan]:
    """Attach selection-delimited speculative frames to the preceding body.

    MTP/Eagle-like bodies are not counted as main layers.  They are attached to
    the previous step only when they are a strict structural sub-observation of
    that step and selection/sample kernels delimit the transition exactly.
    """

    plans: list[StepPlan] = []
    index = 0
    while index < len(frames):
        main = frames[index]
        step_frames = [main]
        cursor = index + 1
        while cursor < len(frames):
            candidate = frames[cursor]
            if not candidate.selection_before:
                if frame_has_primary_attention(candidate):
                    break
            if len(candidate.layers) != 1 and frame_has_primary_attention(candidate):
                break
            if len(candidate.layers) >= len(main.layers):
                break
            if not strict_core_component_substructure(candidate, main):
                break
            step_frames.append(candidate)
            cursor += 1
        reason = "main_body"
        if len(step_frames) > 1:
            reason += "+selection_delimited_speculative_tail"
        plans.append(StepPlan(tuple(step_frames), 1, reason))
        index = cursor
    return rebalance_homogeneous_missing_spec_boundary(plans)


def plan_boundary_sequence(plan: StepPlan) -> tuple[str, ...]:
    return tuple(layer_boundary_key(layer.signature) for layer in plan.main_layers)


def plan_has_external_selection(plan: StepPlan) -> bool:
    return any(frame.selection_before or frame.selection_after for frame in plan.frames)


def recurring_boundary_templates_from_plans(plans: Sequence[StepPlan]) -> tuple[tuple[str, ...], ...]:
    counts = Counter(
        plan_boundary_sequence(plan)
        for plan in plans
        if plan.complete and len(plan.main_layers) > 1 and not plan_has_external_selection(plan)
    )
    return tuple(sequence for sequence, count in counts.items() if count >= 2)


def merge_adjacent_template_fragment_plans(plans: Sequence[StepPlan]) -> list[StepPlan]:
    """Merge adjacent fragments only when they exactly rebuild a recurring step.

    Piecewise kernel-detail row order can split one logical body into adjacent
    fragments that look like separate small steps.  This repair is exact: the
    concatenated boundary sequence must equal an already recurring complete
    template, and no visible selection boundary may sit between fragments.
    """

    templates = sorted(recurring_boundary_templates_from_plans(plans), key=len, reverse=True)
    if not templates:
        return list(plans)
    merged: list[StepPlan] = []
    index = 0
    while index < len(plans):
        current = plans[index]
        if not current.complete or plan_has_external_selection(current):
            merged.append(current)
            index += 1
            continue
        matched_end: int | None = None
        matched_template: tuple[str, ...] | None = None
        for template in templates:
            if len(current.main_layers) >= len(template):
                continue
            sequence: list[str] = []
            frames: list[LayerFrame] = []
            cursor = index
            while cursor < len(plans) and len(sequence) < len(template):
                candidate = plans[cursor]
                if not candidate.complete or plan_has_external_selection(candidate):
                    break
                sequence.extend(plan_boundary_sequence(candidate))
                frames.extend(candidate.frames)
                cursor += 1
            if len(sequence) == len(template) and tuple(sequence) == template and cursor > index + 1:
                matched_end = cursor
                matched_template = template
                break
        if matched_end is None or matched_template is None:
            merged.append(current)
            index += 1
            continue
        window = plans[index:matched_end]
        frames = tuple(frame for plan in window for frame in plan.frames)
        main_frame_count = sum(plan.main_frame_count for plan in window)
        merged.append(
            StepPlan(
                frames=frames,
                main_frame_count=main_frame_count,
                reason=(
                    "main_body+adjacent_template_fragments_merged:"
                    + "+".join(str(len(plan.main_layers)) for plan in window)
                    + f"->{len(matched_template)}"
                ),
            )
        )
        index = matched_end
    return merged


def plan_is_template_prefix_residual(plan: StepPlan) -> bool:
    return bool(plan.frames) and all(frame_has_tag(frame, "exact_template_prefix_residual") for frame in plan.frames)


def plan_is_template_suffix_residual(plan: StepPlan) -> bool:
    return bool(plan.frames) and all(frame_has_tag(frame, "exact_template_suffix_residual") for frame in plan.frames)


def plan_is_template_residual(plan: StepPlan) -> bool:
    return plan_is_template_prefix_residual(plan) or plan_is_template_suffix_residual(plan)


def plan_piecewise_template_cover(plan: StepPlan, templates: Sequence[tuple[str, ...]]) -> list[tuple[int, int, int]] | None:
    if not plan_is_template_residual(plan):
        return None
    frame = LayerFrame(plan.main_layers, reason=plan.reason)
    return piecewise_template_slice_cover(
        frame_boundary_sequence(frame),
        frame_attention_stream_sequence(frame),
        templates,
    )


def classify_residual_plans(plans: Sequence[StepPlan]) -> list[StepPlan]:
    recurring_templates = recurring_boundary_templates_from_plans(
        tuple(plan for plan in plans if not plan_is_template_residual(plan))
    )
    classified: list[StepPlan] = []
    for index, plan in enumerate(plans):
        if not plan_is_template_residual(plan):
            classified.append(plan)
            continue
        piecewise_cover = plan_piecewise_template_cover(plan, recurring_templates)
        if piecewise_cover is not None:
            classified.append(
                StepPlan(
                    frames=plan.frames,
                    main_frame_count=plan.main_frame_count,
                    reason=(
                        f"{plan.reason}+piecewise_interleaved_template_slices:"
                        + ",".join(f"{start}-{end}@{template_start}" for start, end, template_start in piecewise_cover)
                    ),
                    segment_type="piecewise_interleaved_window",
                    complete=False,
                )
            )
            continue
        if index == 0:
            segment_type = "head"
        elif index == len(plans) - 1:
            segment_type = "tail"
        else:
            segment_type = "unclassified_island"
        classified.append(
            StepPlan(
                frames=plan.frames,
                main_frame_count=plan.main_frame_count,
                reason=f"{plan.reason}+classified_template_residual",
                segment_type=segment_type,
                complete=False,
            )
        )
    return classified


def plan_has_primary_attention(plan: StepPlan) -> bool:
    return any(frame_has_primary_attention(frame) for frame in plan.frames)


def classify_edge_no_attention_plans(plans: Sequence[StepPlan]) -> list[StepPlan]:
    """Keep edge runner/dummy-like bodies out of complete step inventory.

    A no-attention body at the file edge after proven attention workloads can be
    graph-capture or runner tail evidence.  It is still materialized, but it is
    not a complete step because there is no rank-local proof of a full forward
    pass boundary.
    """

    first_attention = next((index for index, plan in enumerate(plans) if plan.complete and plan_has_primary_attention(plan)), None)
    if first_attention is None:
        return list(plans)
    last_attention = len(plans) - 1 - next(
        index for index, plan in enumerate(reversed(plans)) if plan.complete and plan_has_primary_attention(plan)
    )
    classified: list[StepPlan] = []
    for index, plan in enumerate(plans):
        if not plan.complete or plan_has_primary_attention(plan):
            classified.append(plan)
            continue
        if index < first_attention:
            classified.append(
                StepPlan(
                    frames=plan.frames,
                    main_frame_count=plan.main_frame_count,
                    reason=f"{plan.reason}+edge_no_attention_head",
                    segment_type="head",
                    complete=False,
                )
            )
            continue
        if index > last_attention:
            classified.append(
                StepPlan(
                    frames=plan.frames,
                    main_frame_count=plan.main_frame_count,
                    reason=f"{plan.reason}+edge_no_attention_tail",
                    segment_type="tail",
                    complete=False,
                )
            )
            continue
        classified.append(plan)
    return classified


def complete_attention_plan(plan: StepPlan) -> bool:
    return plan.complete and plan_has_primary_attention(plan) and len(plan.main_layers) > 1


def surrounded_by_complete_attention(plans: Sequence[StepPlan], index: int) -> bool:
    """Return true only for fragments embedded inside a proven attention run.

    A shorter attention body at the beginning of a profile can be a legitimate
    singleton workload such as a vision encoder.  It must not be demoted just
    because its abstract block roles are a subset of a later LLM step.  The
    substructure repair is therefore limited to bodies with complete attention
    evidence on both sides.
    """

    return any(complete_attention_plan(plan) for plan in plans[:index]) and any(
        complete_attention_plan(plan) for plan in plans[index + 1 :]
    )


def classify_interior_substructure_plans(plans: Sequence[StepPlan]) -> list[StepPlan]:
    """Classify exact sub-windows of a proven body without hiding them."""

    reference_frames = [
        LayerFrame(plan.main_layers, reason=plan.reason)
        for plan in plans
        if plan.complete and plan_has_primary_attention(plan) and len(plan.main_layers) > 1
    ]
    if not reference_frames:
        return list(plans)

    def is_substructure_of_reference(plan: StepPlan) -> bool:
        candidate = LayerFrame(plan.main_layers, reason=plan.reason)
        return any(
            len(plan.main_layers) != len(reference.layers)
            and (
                strict_core_component_substructure(candidate, reference)
                or strict_boundary_substructure(candidate, reference)
            )
            for reference in reference_frames
        )

    classified: list[StepPlan] = []
    for index, plan in enumerate(plans):
        if not plan.complete:
            classified.append(plan)
            continue
        is_substructure = is_substructure_of_reference(plan)
        if is_substructure and (not plan_has_primary_attention(plan) or surrounded_by_complete_attention(plans, index)):
            segment_type = "partial_body_window" if plan_has_primary_attention(plan) else "runner_window"
            classified.append(
                StepPlan(
                    frames=plan.frames,
                    main_frame_count=plan.main_frame_count,
                    reason=f"{plan.reason}+substructure_of_observed_attention_body",
                    segment_type=segment_type,
                    complete=False,
                )
            )
            continue
        classified.append(plan)
    repaired: list[StepPlan] = []
    explained_types = {"partial_body_window", "runner_window"}
    for index, plan in enumerate(classified):
        if (
            plan.complete
            and plan_has_primary_attention(plan)
            and is_substructure_of_reference(plan)
            and (
                (index > 0 and classified[index - 1].segment_type in explained_types)
                or (index + 1 < len(classified) and classified[index + 1].segment_type in explained_types)
            )
        ):
            has_attention_before = any(complete_attention_plan(item) for item in classified[:index])
            has_attention_after = any(complete_attention_plan(item) for item in classified[index + 1 :])
            if not has_attention_before or not has_attention_after:
                segment_type = "head" if not has_attention_before else "tail"
            else:
                segment_type = "partial_body_window"
            repaired.append(
                StepPlan(
                    frames=plan.frames,
                    main_frame_count=plan.main_frame_count,
                    reason=f"{plan.reason}+adjacent_to_explained_substructure_window",
                    segment_type=segment_type,
                    complete=False,
                )
            )
            continue
        if (
            plan.complete
            and len(plan.main_layers) == 1
            and not plan_has_primary_attention(plan)
            and (
                (index > 0 and classified[index - 1].segment_type in explained_types)
                or (index + 1 < len(classified) and classified[index + 1].segment_type in explained_types)
            )
        ):
            repaired.append(
                StepPlan(
                    frames=plan.frames,
                    main_frame_count=plan.main_frame_count,
                    reason=f"{plan.reason}+adjacent_to_explained_substructure_window",
                    segment_type="runner_window",
                    complete=False,
                )
            )
            continue
        repaired.append(plan)
    return repaired


def merge_explained_windows_to_templates(plans: Sequence[StepPlan]) -> list[StepPlan]:
    """Rebuild complete bodies from adjacent explained windows.

    Some piecewise/VL traces expose a complete body as several exact template
    chunks plus a final residual chunk that carries the sampling tail.  Once a
    full template has been observed elsewhere in the same rank, these adjacent
    non-complete windows can be promoted only when their concatenated boundary
    sequence exactly equals that template.
    """

    templates = sorted(
        {
            plan_boundary_sequence(plan)
            for plan in plans
            if plan.complete and len(plan.main_layers) > 1
        },
        key=len,
        reverse=True,
    )
    if not templates:
        return list(plans)
    mergeable_types = {"partial_body_window", "piecewise_interleaved_window", "unclassified_island"}
    merged: list[StepPlan] = []
    index = 0
    while index < len(plans):
        current = plans[index]
        if current.complete or current.segment_type not in mergeable_types:
            merged.append(current)
            index += 1
            continue
        matched_end: int | None = None
        matched_template: tuple[str, ...] | None = None
        for template in templates:
            sequence: list[str] = []
            cursor = index
            while cursor < len(plans) and len(sequence) < len(template):
                candidate = plans[cursor]
                if candidate.complete or candidate.segment_type not in mergeable_types:
                    break
                sequence.extend(plan_boundary_sequence(candidate))
                cursor += 1
            if len(sequence) == len(template) and tuple(sequence) == template and cursor > index + 1:
                matched_end = cursor
                matched_template = template
                break
        if matched_end is None or matched_template is None:
            merged.append(current)
            index += 1
            continue
        window = plans[index:matched_end]
        frames = tuple(frame for plan in window for frame in plan.frames)
        merged.append(
            StepPlan(
                frames=frames,
                main_frame_count=sum(plan.main_frame_count for plan in window),
                reason=(
                    "main_body+explained_windows_merged_to_template:"
                    + "+".join(str(len(plan.main_layers)) for plan in window)
                    + f"->{len(matched_template)}"
                ),
            )
        )
        index = matched_end
    return merged


def plan_speculative_layer_count(plan: StepPlan) -> int:
    return len(plan.all_layers) - len(plan.main_layers)


def singleton_tail_role(plan: StepPlan) -> str | None:
    tail_frames = plan.frames[plan.main_frame_count :]
    if not tail_frames or not all(len(frame.layers) == 1 for frame in tail_frames):
        return None
    roles = {frame.role_sequence[0] for frame in tail_frames}
    return next(iter(roles)) if len(roles) == 1 else None


def split_frame_suffix(frame: LayerFrame, suffix_len: int, *, reason: str) -> tuple[LayerFrame, LayerFrame]:
    prefix = tuple(frame.layers[:-suffix_len])
    suffix = tuple(frame.layers[-suffix_len:])
    return (
        LayerFrame(
            layers=prefix,
            reason=frame.reason,
            selection_before=frame.selection_before,
            selection_after=(),
            tags=frame.tags,
        ),
        LayerFrame(
            layers=suffix,
            reason=reason,
            selection_before=(),
            selection_after=frame.selection_after,
            tags=frame.tags,
        ),
    )


def layer_has_selection(layer: LayerObservation) -> bool:
    return "selection" in layer.signature.split("|")


def rebalance_homogeneous_missing_spec_boundary(plans: Sequence[StepPlan]) -> list[StepPlan]:
    """Recover a missing main-to-spec boundary from exact peer evidence.

    Graph/dummy paths can hide the first speculative boundary, producing
    ``main+1, spec, spec`` while the same rank also shows ``main, spec, spec,
    spec`` for the same total step length.  The repair is allowed only when the
    missing suffix layers have the same role key as the visible singleton spec
    tail and the target spec count is already observed in another exact step.
    """

    observed_spec_by_total: dict[int, Counter[int]] = {}
    for plan in plans:
        spec_count = plan_speculative_layer_count(plan)
        if spec_count <= 0 or singleton_tail_role(plan) is None:
            continue
        observed_spec_by_total.setdefault(len(plan.all_layers), Counter())[spec_count] += 1

    if not observed_spec_by_total:
        return list(plans)

    adjusted: list[StepPlan] = []
    for plan in plans:
        spec_count = plan_speculative_layer_count(plan)
        tail_role = singleton_tail_role(plan)
        first_frame = plan.frames[0] if plan.frames else None
        total = len(plan.all_layers)
        candidates = sorted(count for count in observed_spec_by_total.get(total, Counter()) if count > spec_count)
        if (
            not candidates
            and spec_count > 0
            and tail_role is not None
            and first_frame is not None
            and not frame_has_primary_attention(first_frame)
            and len(first_frame.layers) > 1
            and layer_has_selection(first_frame.layers[-1])
            and layer_role_key(first_frame.layers[-1].signature) == tail_role
        ):
            candidates = [spec_count + 1]
        if (
            spec_count <= 0
            or tail_role is None
            or first_frame is None
            or not candidates
            or len(first_frame.layers) <= 1
        ):
            adjusted.append(plan)
            continue
        target_spec = candidates[0]
        move_count = target_spec - spec_count
        if move_count <= 0 or move_count >= len(first_frame.layers):
            adjusted.append(plan)
            continue
        suffix_roles = tuple(layer_role_key(layer.signature) for layer in first_frame.layers[-move_count:])
        if any(role != tail_role for role in suffix_roles):
            adjusted.append(plan)
            continue
        prefix_frame, suffix_frame = split_frame_suffix(
            first_frame,
            move_count,
            reason=f"{first_frame.reason}+missing_spec_boundary_recovered_by_peer_step",
        )
        adjusted.append(
            StepPlan(
                frames=(prefix_frame, suffix_frame, *plan.frames[1:]),
                main_frame_count=1,
                reason=f"{plan.reason}+missing_spec_boundary_recovered_by_peer_step",
            )
        )
    return adjusted


def step_family_for_events(events: Sequence[NormalizedEvent]) -> str:
    roles = Counter(role for event in events for role in event.op_roles)
    if roles.get("attention") and roles.get("moe"):
        return "attention_moe_workload"
    if roles.get("attention") and roles.get("compute"):
        return "attention_dense_workload"
    if roles.get("attention"):
        return "attention_workload"
    if roles.get("moe"):
        return "moe_or_dummy_workload"
    if roles.get("communication") and not roles.get("compute"):
        return "communication_or_dummy_workload"
    if roles.get("compute"):
        return "compute_workload"
    return "unclassified_workload"


def step_structure_signature(plan: StepPlan) -> str:
    main_sequence = tuple(layer.regime_key for layer in plan.main_layers)
    spec_sequence = tuple(layer.regime_key for frame in plan.frames[1:] for layer in frame.layers)
    main_period = minimal_exact_period(main_sequence)
    spec_period = minimal_exact_period(spec_sequence)
    parts = [f"main_len={len(main_sequence)}", "main_period=" + "||".join(main_period)]
    if spec_sequence:
        parts.append(f"spec_len={len(spec_sequence)}")
        parts.append("spec_period=" + "||".join(spec_period))
    return " ; ".join(parts)


def validate_exact_cover(rank_id: str, events: Sequence[NormalizedEvent], row_numbers: Sequence[int], plans: Sequence[StepPlan]) -> list[dict[str, Any]]:
    if not plans:
        return []
    hard_errors: list[dict[str, Any]] = []
    first_step_start = plans[0].frames[0].row_start
    last_step_end = plans[-1].frames[-1].row_end
    covered_intervals = sorted((layer.row_start, layer.row_end) for plan in plans for layer in plan.all_layers)
    interval_index = 0
    uncovered_structural: list[NormalizedEvent] = []
    for event in events:
        if event.row_idx < first_step_start or event.row_idx > last_step_end or not structural_event(event):
            continue
        while interval_index < len(covered_intervals) and covered_intervals[interval_index][1] < event.row_idx:
            interval_index += 1
        if interval_index >= len(covered_intervals):
            uncovered_structural.append(event)
            continue
        interval_start, interval_end = covered_intervals[interval_index]
        if not (interval_start <= event.row_idx <= interval_end):
            uncovered_structural.append(event)
    structural_middle = [
        event
        for event in uncovered_structural
    ]
    if structural_middle:
        hard_errors.append(
            {
                "rank_id": rank_id,
                "error_type": "unexplained_structural_middle",
                "row_start": structural_middle[0].row_idx,
                "row_end": structural_middle[-1].row_idx,
                "examples": [
                    {
                        "row_idx": event.row_idx,
                        "name": event.name_raw,
                        "roles": list(event.op_roles),
                        "categories": list(event.op_categories),
                    }
                    for event in structural_middle[:16]
                ],
            }
        )
    previous_end = first_step_start - 1
    for plan in plans:
        current_start = plan.frames[0].row_start
        if current_start <= previous_end:
            hard_errors.append(
                {
                    "rank_id": rank_id,
                    "error_type": "overlapping_step_plan",
                    "row_start": current_start,
                    "previous_end": previous_end,
                }
            )
        previous_end = plan.frames[-1].row_end
    complete_plans = [plan for plan in plans if plan.complete]
    hard_errors.extend(validate_embedded_short_steps(rank_id, complete_plans))
    hard_errors.extend(validate_unresolved_composite_bodies(rank_id, complete_plans))
    for index, plan in enumerate(plans):
        if plan.complete or plan.segment_type in {"head", "tail"}:
            continue
        if plan.segment_type in {"piecewise_interleaved_window", "runner_window", "partial_body_window"}:
            continue
        hard_errors.append(
            {
                "rank_id": rank_id,
                "error_type": "interior_template_residual",
                "row_start": plan.frames[0].row_start,
                "row_end": plan.frames[-1].row_end,
                "observed_layers": len(plan.main_layers),
                "previous_rows": [plans[index - 1].frames[0].row_start, plans[index - 1].frames[-1].row_end] if index > 0 else None,
                "next_rows": [plans[index + 1].frames[0].row_start, plans[index + 1].frames[-1].row_end] if index + 1 < len(plans) else None,
                "summary": "A template residual appears between complete bodies. This must be explained before the profile is trusted.",
            }
        )
    _ = row_numbers  # retained for symmetric call sites and future source refs
    return hard_errors


def sequence_occurrence_count(sequence: Sequence[str], template: Sequence[str]) -> int:
    if not sequence or not template or len(template) > len(sequence):
        return 0
    template_tuple = tuple(template)
    return sum(1 for index in range(0, len(sequence) - len(template) + 1) if tuple(sequence[index : index + len(template)]) == template_tuple)


def validate_unresolved_composite_bodies(rank_id: str, plans: Sequence[StepPlan]) -> list[dict[str, Any]]:
    sequence_counts = Counter(
        tuple(core_role_key(layer.signature) for layer in plan.main_layers)
        for plan in plans
        if len(plan.main_layers) > 1
    )
    boundary_sequence_counts = Counter(
        tuple(layer_boundary_key(layer.signature) for layer in plan.main_layers)
        for plan in plans
        if len(plan.main_layers) > 1
    )
    templates = {sequence for sequence, count in sequence_counts.items() if count >= 2}
    boundary_templates = {sequence for sequence, count in boundary_sequence_counts.items() if count >= 2}
    errors: list[dict[str, Any]] = []
    for plan in plans:
        sequence = tuple(core_role_key(layer.signature) for layer in plan.main_layers)
        smaller_templates = tuple(template for template in templates if 0 < len(template) < len(sequence))
        if not smaller_templates:
            continue
        occurrences = {
            len(template): count
            for template in smaller_templates
            if (count := sequence_occurrence_count(sequence, template)) > 0
        }
        if not occurrences:
            continue
        if exact_cover_sequence(sequence, smaller_templates) is not None:
            continue
        boundary_sequence = tuple(layer_boundary_key(layer.signature) for layer in plan.main_layers)
        smaller_boundary_templates = tuple(template for template in boundary_templates if 0 < len(template) < len(boundary_sequence))
        if exact_cover_sequence(boundary_sequence, smaller_boundary_templates) is not None:
            continue
        errors.append(
            {
                "rank_id": rank_id,
                "error_type": "unresolved_composite_body",
                "row_start": plan.frames[0].row_start,
                "row_end": plan.frames[-1].row_end,
                "current_layers": len(plan.main_layers),
                "embedded_template_lengths": sorted(occurrences),
                "embedded_template_occurrences": occurrences,
                "summary": (
                    "The body contains complete observed step templates but cannot be "
                    "losslessly covered by them. Reporting it as one complete step would be misleading."
                ),
            }
        )
    return errors


def validate_embedded_short_steps(rank_id: str, plans: Sequence[StepPlan]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for index in range(1, len(plans) - 1):
        previous = plans[index - 1]
        current = plans[index]
        next_plan = plans[index + 1]
        previous_count = len(previous.main_layers)
        next_count = len(next_plan.main_layers)
        current_count = len(current.main_layers)
        if previous_count != next_count or current_count >= previous_count:
            continue
        previous_sequence = tuple(layer.regime_key for layer in previous.main_layers)
        current_sequence = tuple(layer.regime_key for layer in current.main_layers)
        next_sequence = tuple(layer.regime_key for layer in next_plan.main_layers)
        if previous_sequence != next_sequence:
            continue
        if not strict_substructure(current_sequence, previous_sequence):
            continue
        errors.append(
            {
                "rank_id": rank_id,
                "error_type": "embedded_short_body_between_peer_steps",
                "row_start": current.frames[0].row_start,
                "row_end": current.frames[-1].row_end,
                "current_layers": current_count,
                "peer_layers": previous_count,
                "previous_rows": [previous.frames[0].row_start, previous.frames[-1].row_end],
                "next_rows": [next_plan.frames[0].row_start, next_plan.frames[-1].row_end],
                "summary": (
                    "A structurally shorter body is embedded between two equal peer steps. "
                    "Kernel details alone cannot prove a complete step here."
                ),
            }
        )
    return errors


def add_evidence(
    evidence: list[EvidenceRef],
    *,
    evidence_id: str,
    kind: str,
    summary: str,
    events: Sequence[NormalizedEvent],
    segment_id: str | None = None,
    layer_id: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
    evidence.append(
        EvidenceRef(
            evidence_id=evidence_id,
            kind=kind,
            summary=summary,
            event_ids=tuple(event.event_id for event in events[:64]),
            segment_ids=(segment_id,) if segment_id else (),
            layer_ids=(layer_id,) if layer_id else (),
            metrics=metrics or {},
        )
    )


def build_segments_for_rank(rank_id: str, events: Sequence[NormalizedEvent]) -> tuple[list[StepSegment], list[LayerSegment], list[StructureObservation], list[EvidenceRef], list[dict[str, Any]]]:
    if not events:
        return [], [], [], [], []
    events = sorted(events, key=lambda event: event.row_idx)
    row_numbers = tuple(event.row_idx for event in events)
    boundary_rows = dedup_adjacent_event_rows(
        events,
        lambda event: event_role(event, "block_head") or event_role(event, "normalization"),
    )
    selection_rows = dedup_adjacent_event_rows(events, lambda event: event_role(event, "selection"))
    layers_observed = build_layers(events, row_numbers, boundary_rows, selection_rows)
    evidence: list[EvidenceRef] = []
    segments: list[StepSegment] = []
    layer_segments: list[LayerSegment] = []
    observations: list[StructureObservation] = []
    hard_errors: list[dict[str, Any]] = []

    if not layers_observed:
        segment_id = stable_id("seg", rank_id, "no_structural_layers", events[0].row_idx, events[-1].row_idx)
        evidence_id = stable_id("evd", segment_id, "no_structural_layers")
        add_evidence(
            evidence,
            evidence_id=evidence_id,
            kind="rank_window",
            summary="No structural layer evidence was found in this rank.",
            events=events,
            segment_id=segment_id,
            metrics=metrics_for_events(events, top_gap_limit=3),
        )
        segments.append(
            StepSegment(
                segment_id=segment_id,
                rank_id=rank_id,
                segment_type="unclassified_island",
                complete=False,
                row_start=events[0].row_idx,
                row_end=events[-1].row_idx,
                start_us=min(event.start_us for event in events),
                end_us=max(event.end_us for event in events),
                evidence_ids=(evidence_id,),
            )
        )
        return segments, layer_segments, observations, evidence, hard_errors

    frames = split_composite_frames(frames_from_selection(layers_observed, selection_rows), events, row_numbers)
    plans = merge_explained_windows_to_templates(
        classify_interior_substructure_plans(
            classify_edge_no_attention_plans(
                classify_residual_plans(merge_adjacent_template_fragment_plans(compose_step_plans(frames)))
            )
        )
    )
    hard_errors.extend(validate_exact_cover(rank_id, events, row_numbers, plans))

    first_step_start = plans[0].frames[0].row_start if plans else events[0].row_idx
    if first_step_start > events[0].row_idx:
        head_events = event_slice(events, row_numbers, events[0].row_idx, first_step_start - 1)
        if head_events:
            head_id = stable_id("seg", rank_id, "head", events[0].row_idx, first_step_start - 1)
            evidence_id = stable_id("evd", head_id, "head")
            add_evidence(
                evidence,
                evidence_id=evidence_id,
                kind="edge_window",
                summary="Rank head before first exact structural step.",
                events=head_events,
                segment_id=head_id,
                metrics=metrics_for_events(head_events, top_gap_limit=3),
            )
            segments.append(
                StepSegment(
                    segment_id=head_id,
                    rank_id=rank_id,
                    segment_type="head",
                    complete=False,
                    row_start=events[0].row_idx,
                    row_end=first_step_start - 1,
                    start_us=min(event.start_us for event in head_events),
                    end_us=max(event.end_us for event in head_events),
                    evidence_ids=(evidence_id,),
                )
            )

    for plan_index, plan in enumerate(plans):
        main_layers = plan.main_layers
        all_layers = plan.all_layers
        if not main_layers or not all_layers:
            continue
        step_start = all_layers[0].row_start
        if plan_index + 1 < len(plans):
            step_end = plans[plan_index + 1].frames[0].row_start - 1
        else:
            step_end = events[-1].row_idx
        step_events = event_slice(events, row_numbers, step_start, step_end)
        if not plan.complete:
            segment_id = stable_id("seg", rank_id, plan.segment_type, plan_index, step_start, step_end)
            evidence_id = stable_id("evd", segment_id, "template_residual")
            add_evidence(
                evidence,
                evidence_id=evidence_id,
                kind="edge_window" if plan.segment_type in {"head", "tail"} else "rank_window",
                summary=(
                    "Template residual rows retained for review. They are not reported "
                    "as a complete structural step."
                ),
                events=step_events,
                segment_id=segment_id,
                metrics={
                    **metrics_for_events(step_events, top_gap_limit=3),
                    "observed_layer_count": len(main_layers),
                    "frame_reasons": [frame.reason for frame in plan.frames],
                    "frame_tags": [list(frame.tags) for frame in plan.frames],
                },
            )
            segments.append(
                StepSegment(
                    segment_id=segment_id,
                    rank_id=rank_id,
                    segment_type=plan.segment_type,
                    complete=False,
                    row_start=step_start,
                    row_end=step_end,
                    start_us=min((event.start_us for event in step_events), default=all_layers[0].anchors[0].start_us),
                    end_us=max((event.end_us for event in step_events), default=all_layers[-1].anchors[-1].end_us),
                    evidence_ids=(evidence_id,),
                )
            )
            continue
        family = step_family_for_events(step_events)
        main_layer_count = len(main_layers)
        speculative_layer_count = len(all_layers) - len(main_layers)
        signature = step_structure_signature(plan)
        cluster_id = stable_id("cluster", rank_id, family, main_layer_count, speculative_layer_count, signature, length=12)
        segment_id = stable_id("seg", rank_id, "step", plan_index, step_start, step_end)
        evidence_id = stable_id("evd", segment_id, "exact_step_cover")
        layer_ids: list[str] = []

        for layer_index, layer in enumerate(all_layers):
            layer_start = layer.row_start
            if layer_index + 1 < len(all_layers):
                layer_end = all_layers[layer_index + 1].row_start - 1
            else:
                layer_end = step_end
            layer_events = event_slice(events, row_numbers, layer_start, layer_end)
            layer_id = stable_id("layer", segment_id, layer_index, layer_start, layer_end)
            layer_evidence_id = stable_id("evd", layer_id, "exact_layer_window")
            layer_ids.append(layer_id)
            role = "main" if layer_index < main_layer_count else "speculative"
            add_evidence(
                evidence,
                evidence_id=layer_evidence_id,
                kind="layer_window",
                summary=f"Exact layer observation from {', '.join(anchor_signature(anchor) for anchor in layer.anchors)}.",
                events=layer_events,
                segment_id=segment_id,
                layer_id=layer_id,
                metrics=metrics_for_events(layer_events, top_gap_limit=0),
            )
            layer_segments.append(
                LayerSegment(
                    layer_id=layer_id,
                    rank_id=rank_id,
                    segment_id=segment_id,
                    layer_index=layer_index,
                    layer_role=role,
                    boundary_source="exact_structural_anchor",
                    row_start=layer_start,
                    row_end=layer_end,
                    start_us=min((event.start_us for event in layer_events), default=layer.anchors[0].start_us),
                    end_us=max((event.end_us for event in layer_events), default=layer.anchors[-1].end_us),
                    structure_signature=layer.signature,
                    evidence_ids=(layer_evidence_id,),
                )
            )

        add_evidence(
            evidence,
            evidence_id=evidence_id,
            kind="step_window",
            summary=(
                f"Exact step cover: main_layers={main_layer_count}, "
                f"speculative_layers={speculative_layer_count}, reason={plan.reason}."
            ),
            events=step_events,
            segment_id=segment_id,
            metrics={
                **metrics_for_events(step_events, top_gap_limit=5),
                "main_layer_count": main_layer_count,
                "speculative_layer_count": speculative_layer_count,
                "frame_count": len(plan.frames),
                "frame_reasons": [frame.reason for frame in plan.frames],
                "frame_tags": [list(frame.tags) for frame in plan.frames],
                "selection_rows": [row for frame in plan.frames for row in (*frame.selection_before, *frame.selection_after)],
            },
        )
        segments.append(
            StepSegment(
                segment_id=segment_id,
                rank_id=rank_id,
                segment_type="step",
                complete=True,
                row_start=step_start,
                row_end=step_end,
                start_us=min((event.start_us for event in step_events), default=all_layers[0].anchors[0].start_us),
                end_us=max((event.end_us for event in step_events), default=all_layers[-1].anchors[-1].end_us),
                cluster_id=cluster_id,
                step_family=family,
                main_layer_count=main_layer_count,
                speculative_layer_count=speculative_layer_count,
                structure_signature=signature,
                layer_ids=tuple(layer_ids),
                evidence_ids=(evidence_id,),
            )
        )

        role_counts = Counter(role for event in step_events for role in event.op_roles)
        for role, count in role_counts.items():
            obs_id = stable_id("struct", segment_id, role)
            observations.append(
                StructureObservation(
                    structure_id=obs_id,
                    scope_type="step",
                    rank_id=rank_id,
                    segment_id=segment_id,
                    role=role,
                    role_family=role.split(".")[0],
                    implementation_evidence=tuple(sorted({event.name_raw for event in step_events if role in event.op_roles})[:16]),
                    event_ids=tuple(event.event_id for event in step_events if role in event.op_roles)[:64],
                    evidence_ids=(evidence_id,),
                    confidence="high" if count > 0 else "low",
                )
            )

    return segments, layer_segments, observations, evidence, hard_errors


def interior_unclassified_segments(segments: Sequence[StepSegment]) -> list[StepSegment]:
    first_step = next((index for index, segment in enumerate(segments) if segment.segment_type == "step"), None)
    if first_step is None:
        return []
    last_from_end = next((index for index, segment in enumerate(reversed(segments)) if segment.segment_type == "step"), None)
    if last_from_end is None:
        return []
    last_step = len(segments) - 1 - last_from_end
    return [
        segment
        for index, segment in enumerate(segments)
        if first_step < index < last_step and segment.segment_type == "unclassified_island"
    ]


def segment_profile(output_dir: Path) -> dict[str, Any]:
    event_path = output_dir / "normalized_event_index.jsonl"
    events = load_events(event_path)
    grouped = group_by_rank(events)
    all_segments: list[StepSegment] = []
    all_layers: list[LayerSegment] = []
    all_observations: list[StructureObservation] = []
    all_evidence: list[EvidenceRef] = []
    rank_summaries: list[dict[str, Any]] = []
    hard_errors: list[dict[str, Any]] = []
    for rank_id, rank_events in grouped.items():
        segments, layers, observations, evidence, rank_errors = build_segments_for_rank(rank_id, rank_events)
        interior_islands = interior_unclassified_segments(segments)
        if interior_islands:
            rank_errors.append(
                {
                    "rank_id": rank_id,
                    "error_type": "interior_unclassified_island",
                    "count": len(interior_islands),
                    "examples": [
                        {
                            "segment_id": segment.segment_id,
                            "row_start": segment.row_start,
                            "row_end": segment.row_end,
                            "structure_signature": segment.structure_signature,
                        }
                        for segment in interior_islands[:8]
                    ],
                }
            )
        hard_errors.extend(rank_errors)
        all_segments.extend(segments)
        all_layers.extend(layers)
        all_observations.extend(observations)
        all_evidence.extend(evidence)
        rank_summaries.append(
            {
                "rank_id": rank_id,
                "event_count": len(rank_events),
                "segment_count": len(segments),
                "step_count": sum(1 for segment in segments if segment.segment_type == "step"),
                "interior_unclassified_count": len(interior_islands),
                "layer_count_inventory": sorted(
                    {
                        segment.main_layer_count
                        for segment in segments
                        if segment.segment_type == "step" and segment.main_layer_count is not None
                    }
                ),
                "hard_error_count": len(rank_errors),
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "segment",
        "created_at": utc_now(),
        "output_dir": str(output_dir),
        "files": {
            "step_segments": "step_segments.json",
            "layer_segments": "layer_segments.json",
            "structure_evidence_graph": "structure_evidence_graph.json",
            "segment_manifest": "segment_manifest.json",
        },
        "rank_summaries": rank_summaries,
        "segment_count": len(all_segments),
        "layer_count": len(all_layers),
        "structure_observation_count": len(all_observations),
        "evidence_count": len(all_evidence),
        "hard_errors": hard_errors,
    }
    write_json(output_dir / "step_segments.json", {"step_segments": all_segments})
    write_json(output_dir / "layer_segments.json", {"layer_segments": all_layers})
    write_json(
        output_dir / "structure_evidence_graph.json",
        {
            "structure_observations": all_observations,
            "evidence": all_evidence,
        },
    )
    write_json(output_dir / "segment_manifest.json", manifest)
    if hard_errors:
        summary = "; ".join(f"{item.get('rank_id')}:{item.get('error_type')}" for item in hard_errors[:8])
        raise RuntimeError(f"segment exact-cover validation failed: {summary}")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="analysis output directory containing normalized_event_index.jsonl/csv")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = segment_profile(Path(args.output))
    print({"stage": "segment", "segment_count": manifest["segment_count"], "layer_count": manifest["layer_count"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
