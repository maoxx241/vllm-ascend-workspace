#!/usr/bin/env python3
"""Block decomposition + step/layer/block class signatures.

This stage runs between ``segment`` and ``summarize``.  It consumes the
already-decomposed steps and layers and:

1. Splits every layer into one or more **blocks** of kind
   ``attention`` / ``ffn`` / ``moe`` / ``aicpu`` / ``other``.  A
   conventional vLLM transformer layer becomes one ``attention`` plus one
   ``ffn`` (or ``moe``) block.  Layers that have no attention kernel are
   flagged as ``companion_layer`` so the report can call them out
   separately rather than silently mixing dummy/eager-mode passes into
   the main analysis.

2. Computes **shape-strict class signatures** for steps, layers, and
   blocks.  Two members share a class iff they have the same structure
   signature *and* the same ordered list of ``(normalized_op_name,
   shape_signature)`` pairs.  Members with no shape-bearing events get a
   unique singleton id (``*_unknown_shape_<member_id>``) so we never
   merge missing-data members into a real class.

The classifier writes:
    - ``block_segments.json`` — every block with parent ids and class id.
    - ``class_signatures.json`` — per-class member lists and metadata.

The block decomposition is **purely role-driven**: roles are taken from
``op_roles`` (which is a normalised label, never a raw kernel name), and
the only name-based heuristic is the ``mix_comm_aiv`` op_type override
that pulls fused ``DispatchFFNCombine`` style kernels into the ``moe``
block.  See ``knowledge/block_taxonomy.md`` for the rules.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .common import (
        BlockSegment,
        LayerSegment,
        NormalizedEvent,
        SCHEMA_VERSION,
        StepSegment,
        TOOL_VERSION,
        emit_stage_json,
        group_by_rank,
        load_events,
        load_layer_segments,
        load_step_segments,
        stable_id,
        utc_now,
        write_json,
    )
except ImportError:  # pragma: no cover - script-mode fallback
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import (
        # type: ignore[no-redef]
        BlockSegment,
        LayerSegment,
        NormalizedEvent,
        SCHEMA_VERSION,
        StepSegment,
        TOOL_VERSION,
        emit_stage_json,
        group_by_rank,
        load_events,
        load_layer_segments,
        load_step_segments,
        stable_id,
        utc_now,
        write_json,
    )


BLOCK_KINDS: tuple[str, ...] = ("attention", "ffn", "moe", "aicpu", "other")
HARD_KINDS: frozenset[str] = frozenset({"attention", "ffn", "moe"})


def _normalized_name_key(name: str) -> str:
    """Strip kernel-id digits/hex so call_count families stay merged.

    Mirrors ``segment.normalized_name_key`` so step/layer/block class
    signatures are aligned with the layer-period structure_signature
    convention.  Kept inline here to avoid a circular import.
    """

    text = name.lower()
    text = re.sub(r"0x[0-9a-f]+", "", text)
    text = re.sub(r"[0-9a-f]{16,}", "", text)
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"[^a-z_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:96] or "unknown"


def _is_attention_event(event: NormalizedEvent) -> bool:
    roles = event.op_roles
    return "attention" in roles or "attention_aux" in roles


def _is_moe_event(event: NormalizedEvent) -> bool:
    if "moe" in event.op_roles:
        return True
    # DispatchFFNCombine / MoeDistributeDispatch / MoeDistributeCombine
    # ride the COMMUNICATION accelerator core; their AIV-side work is
    # conceptually MoE compute, so they anchor the moe block too.
    if event.op_type == "mix_comm_aiv":
        return True
    return False


def _is_aicpu_event(event: NormalizedEvent) -> bool:
    return "aicpu" in event.op_roles or event.op_type == "aicpu"


def _event_slice(
    events: Sequence[NormalizedEvent],
    row_indexes: Sequence[int],
    row_start: int,
    row_end: int,
) -> list[NormalizedEvent]:
    if row_end < row_start:
        return []
    left = bisect.bisect_left(row_indexes, int(row_start))
    right = bisect.bisect_right(row_indexes, int(row_end))
    return list(events[left:right])


def decompose_layer_into_blocks(
    layer: LayerSegment,
    layer_events: Sequence[NormalizedEvent],
) -> list[dict[str, Any]]:
    """Split one layer into ``attention`` + (``ffn`` | ``moe``) blocks.

    The user-facing block taxonomy is intentionally coarse: a transformer
    layer is a sequence of "attention work" followed by "FFN/MoE work",
    so we anchor on attention kernels and MoE kernels and absorb every
    surrounding compute / communication / normalization event into the
    nearest anchor.

    Cases handled:
      * standard dense layer  -> ``[attention, ffn]``
      * standard MoE layer    -> ``[attention, moe]``
      * companion MoE layer   -> ``[moe]``                  (no attention)
      * companion FFN layer   -> ``[ffn]``                  (no attention, no moe)
      * AICPU-only layer      -> ``[aicpu]``                (no AI-Core kernel)
      * attention-only layer  -> ``[attention]``            (rare; e.g. fused passes)

    The split point between attention and ffn/moe is the **midpoint
    between the last attention row and the first ffn/moe anchor row** so
    that QKV / O-projection matmuls land naturally inside the attention
    block while expert / gating matmuls stay inside the moe block.

    Returns a list of dicts, each with keys ``kind`` and ``events``.
    Empty layers return ``[]``.
    """

    sorted_events = sorted(layer_events, key=lambda ev: (ev.row_idx, ev.start_us))
    if not sorted_events:
        return []

    attn_rows = [int(ev.row_idx) for ev in sorted_events if _is_attention_event(ev)]
    moe_rows = [int(ev.row_idx) for ev in sorted_events if _is_moe_event(ev)]
    aicpu_rows = [int(ev.row_idx) for ev in sorted_events if _is_aicpu_event(ev)]

    if not attn_rows and not moe_rows:
        # No transformer-style anchor.  AICPU-dominated layers (sampling,
        # bookkeeping) get an explicit ``aicpu`` block; everything else
        # falls under ``ffn`` so dense compute layers (lm_head, embed)
        # still surface as a single non-attention block.
        if aicpu_rows and len(aicpu_rows) >= max(1, len(sorted_events) // 2):
            return [{"kind": "aicpu", "events": list(sorted_events)}]
        return [{"kind": "ffn", "events": list(sorted_events)}]

    if not attn_rows:
        # No attention kernel -> companion layer.  We surface it as a
        # single moe (preferred) or ffn block so the report can flag
        # those layers separately via ``companion_layer=True``.
        kind = "moe" if moe_rows else "ffn"
        return [{"kind": kind, "events": list(sorted_events)}]

    if not moe_rows:
        # Dense FFN path.  Split: everything up to the last attention
        # row stays in ``attention``; the rest becomes ``ffn``.  This
        # absorbs the QKV projection (above first_attn) and the O
        # projection (below last_attn) into the attention block, and
        # leaves the FFN matmul / SwiGLU pair in the ffn block.
        last_attn = max(attn_rows)
        attn_events = [ev for ev in sorted_events if ev.row_idx <= last_attn]
        ffn_events = [ev for ev in sorted_events if ev.row_idx > last_attn]
        out: list[dict[str, Any]] = [{"kind": "attention", "events": attn_events}]
        if ffn_events:
            out.append({"kind": "ffn", "events": ffn_events})
        return out

    # attention + moe layer.  Find the gap between last attention row
    # and first moe row, and split at the midpoint.  When the ranges
    # interleave (rare; usually means a misclassified op), fall back to
    # the moe range as the boundary.
    last_attn = max(attn_rows)
    first_moe = min(moe_rows)
    if last_attn < first_moe:
        split = (last_attn + first_moe) // 2
    else:
        split = first_moe - 1
    attn_events = [ev for ev in sorted_events if ev.row_idx <= split]
    moe_events = [ev for ev in sorted_events if ev.row_idx > split]
    return [
        {"kind": "attention", "events": attn_events},
        {"kind": "moe", "events": moe_events},
    ]


def _build_block_segments(
    layer: LayerSegment,
    raw_blocks: Sequence[Mapping[str, Any]],
) -> list[BlockSegment]:
    has_attention = any(block["kind"] == "attention" for block in raw_blocks)
    companion = not has_attention
    out: list[BlockSegment] = []
    for block_index, block in enumerate(raw_blocks):
        events = block["events"]
        if not events:
            continue
        row_start = min(int(ev.row_idx) for ev in events)
        row_end = max(int(ev.row_idx) for ev in events)
        start_us = min(float(ev.start_us) for ev in events)
        end_us = max(float(ev.end_us) for ev in events)
        block_id = stable_id(
            "blk", layer.layer_id, block_index, block["kind"], row_start, row_end
        )
        out.append(
            BlockSegment(
                block_id=block_id,
                rank_id=layer.rank_id,
                segment_id=layer.segment_id,
                layer_id=layer.layer_id,
                layer_index=int(layer.layer_index),
                block_index=block_index,
                block_kind=str(block["kind"]),
                companion_layer=companion,
                row_start=row_start,
                row_end=row_end,
                start_us=start_us,
                end_us=end_us,
                event_count=len(events),
                evidence_ids=tuple(layer.evidence_ids),
            )
        )
    return out


def _shape_pairs(events: Iterable[NormalizedEvent]) -> tuple[tuple[str, str], ...]:
    """Ordered list of ``(normalized_name, shape_signature)`` pairs.

    Only events that actually carry a ``shape_signature`` participate in
    the class fingerprint -- shape-less events (e.g. RmsNorm, Argmax)
    are ignored.  Order is the row-index order from kernel_details.csv.
    """

    pairs: list[tuple[str, str]] = []
    for event in sorted(events, key=lambda ev: (ev.row_idx, ev.start_us)):
        sig = event.shape_signature
        if not sig:
            continue
        pairs.append((_normalized_name_key(event.name_raw), str(sig)))
    return tuple(pairs)


def _class_id(
    prefix: str,
    structure_sig: str | None,
    scope_label: str | None,
    pairs: Sequence[tuple[str, str]],
    fallback_member_id: str,
) -> tuple[str, bool]:
    """Deterministic class id for a (structure, kind, ordered shapes) tuple.

    Returns ``(class_id, has_shape)``.  Members with no shape pairs get a
    unique ``*_unknown_shape_*`` id so they never merge into a real
    class.
    """

    if not pairs:
        digest = hashlib.blake2b(fallback_member_id.encode("utf-8"), digest_size=6).hexdigest()
        return f"{prefix}_unknown_shape_{digest}", False
    payload = json.dumps(
        [structure_sig or "", scope_label or "", list(pairs)],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()
    return f"{prefix}_{digest}", True


def _row_indexes_by_rank(
    events_by_rank: Mapping[str, Sequence[NormalizedEvent]],
) -> dict[str, list[int]]:
    return {rank: [event.row_idx for event in events] for rank, events in events_by_rank.items()}


def classify_profile(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    events = load_events(output_dir / "normalized_event_index.jsonl")
    events_by_rank = group_by_rank(events)
    row_indexes = _row_indexes_by_rank(events_by_rank)
    steps = load_step_segments(output_dir / "step_segments.json")
    layers = load_layer_segments(output_dir / "layer_segments.json")

    layers_by_step: dict[str, list[LayerSegment]] = defaultdict(list)
    for layer in layers:
        layers_by_step[str(layer.segment_id)].append(layer)
    for items in layers_by_step.values():
        items.sort(key=lambda layer: (float(layer.start_us), int(layer.row_start)))

    block_segments: list[BlockSegment] = []
    blocks_by_layer: dict[str, list[BlockSegment]] = {}
    block_events: dict[str, list[NormalizedEvent]] = {}
    layer_events: dict[str, list[NormalizedEvent]] = {}

    for layer in layers:
        rank_events = events_by_rank.get(layer.rank_id, [])
        rank_rows = row_indexes.get(layer.rank_id, [])
        events_in_layer = _event_slice(rank_events, rank_rows, layer.row_start, layer.row_end)
        layer_events[layer.layer_id] = events_in_layer
        raw_blocks = decompose_layer_into_blocks(layer, events_in_layer)
        layer_blocks = _build_block_segments(layer, raw_blocks)
        blocks_by_layer[layer.layer_id] = layer_blocks
        block_segments.extend(layer_blocks)
        # Record events per block (using row range, since raw_blocks event lists are
        # still aligned with our slicing rule).
        for block, raw in zip(layer_blocks, [b for b in raw_blocks if b["events"]]):
            block_events[block.block_id] = list(raw["events"])

    # ---- Class signatures -------------------------------------------------
    step_classes: dict[str, dict[str, Any]] = {}
    step_class_by_id: dict[str, str] = {}
    layer_classes: dict[str, dict[str, Any]] = {}
    layer_class_by_id: dict[str, str] = {}
    block_classes: dict[str, dict[str, Any]] = {}
    block_class_by_id: dict[str, str] = {}

    # Block classes are computed first so layer classes can reference them.
    for block in block_segments:
        evs = block_events.get(block.block_id, [])
        pairs = _shape_pairs(evs)
        class_id, has_shape = _class_id(
            "blk_cls",
            None,  # block class is structure-agnostic; the layer class carries structure
            f"{block.block_kind}|companion={int(block.companion_layer)}",
            pairs,
            block.block_id,
        )
        block_class_by_id[block.block_id] = class_id
        entry = block_classes.setdefault(
            class_id,
            {
                "block_kind": block.block_kind,
                "companion_layer": block.companion_layer,
                "members": [],
                "has_unknown_shape": not has_shape,
                "shape_pairs_count": len(pairs),
            },
        )
        entry["members"].append(block.block_id)

    for layer in layers:
        layer_blocks = blocks_by_layer.get(layer.layer_id, [])
        evs = layer_events.get(layer.layer_id, [])
        pairs = _shape_pairs(evs)
        block_kinds = tuple(b.block_kind for b in layer_blocks)
        block_class_ids = tuple(block_class_by_id.get(b.block_id, "") for b in layer_blocks)
        scope = f"{'->'.join(block_kinds) or 'empty'}|companion={int(any(b.companion_layer for b in layer_blocks))}"
        class_id, has_shape = _class_id(
            "lyr_cls",
            layer.structure_signature,
            scope,
            pairs,
            layer.layer_id,
        )
        layer_class_by_id[layer.layer_id] = class_id
        entry = layer_classes.setdefault(
            class_id,
            {
                "structure_signature": layer.structure_signature,
                "block_kinds": list(block_kinds),
                "block_class_ids": list(block_class_ids),
                "companion_layer": bool(any(b.companion_layer for b in layer_blocks)),
                "members": [],
                "has_unknown_shape": not has_shape,
                "shape_pairs_count": len(pairs),
            },
        )
        entry["members"].append(layer.layer_id)

    for step in steps:
        if step.segment_type != "step":
            continue
        rank_events = events_by_rank.get(step.rank_id, [])
        rank_rows = row_indexes.get(step.rank_id, [])
        evs = _event_slice(rank_events, rank_rows, step.row_start, step.row_end)
        pairs = _shape_pairs(evs)
        layer_ids = [lyr.layer_id for lyr in layers_by_step.get(str(step.segment_id), [])]
        layer_class_ids = tuple(layer_class_by_id.get(lid, "") for lid in layer_ids)
        scope = f"layers={len(layer_ids)}|main={step.main_layer_count or 0}"
        class_id, has_shape = _class_id(
            "stp_cls",
            step.structure_signature,
            scope,
            pairs,
            step.segment_id,
        )
        step_class_by_id[step.segment_id] = class_id
        entry = step_classes.setdefault(
            class_id,
            {
                "structure_signature": step.structure_signature,
                "main_layer_count": step.main_layer_count,
                "step_family": step.step_family,
                "layer_count": len(layer_ids),
                "layer_class_ids": list(layer_class_ids),
                "members": [],
                "has_unknown_shape": not has_shape,
                "shape_pairs_count": len(pairs),
            },
        )
        entry["members"].append(step.segment_id)

    # ---- Persist ----------------------------------------------------------
    block_segments_payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": utc_now(),
        "block_segments": [
            {
                "block_id": block.block_id,
                "rank_id": block.rank_id,
                "segment_id": block.segment_id,
                "layer_id": block.layer_id,
                "layer_index": block.layer_index,
                "block_index": block.block_index,
                "block_kind": block.block_kind,
                "companion_layer": block.companion_layer,
                "row_start": block.row_start,
                "row_end": block.row_end,
                "start_us": block.start_us,
                "end_us": block.end_us,
                "event_count": block.event_count,
                "block_class_id": block_class_by_id.get(block.block_id),
                "evidence_ids": list(block.evidence_ids),
            }
            for block in block_segments
        ],
    }
    write_json(output_dir / "block_segments.json", block_segments_payload)

    class_signatures_payload = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": utc_now(),
        "step_class_by_id": step_class_by_id,
        "layer_class_by_id": layer_class_by_id,
        "block_class_by_id": block_class_by_id,
        "step_classes": step_classes,
        "layer_classes": layer_classes,
        "block_classes": block_classes,
    }
    write_json(output_dir / "class_signatures.json", class_signatures_payload)

    # ---- Manifest ---------------------------------------------------------
    companion_layer_count = sum(1 for layer_blocks in blocks_by_layer.values() if layer_blocks and any(b.companion_layer for b in layer_blocks))
    block_kind_counts: dict[str, int] = defaultdict(int)
    for block in block_segments:
        block_kind_counts[block.block_kind] += 1

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "classify",
        "created_at": utc_now(),
        "output_dir": str(output_dir),
        "files": {
            "block_segments": "block_segments.json",
            "class_signatures": "class_signatures.json",
        },
        "counts": {
            "blocks": len(block_segments),
            "layers_with_blocks": sum(1 for v in blocks_by_layer.values() if v),
            "companion_layers": companion_layer_count,
            "step_classes": len(step_classes),
            "layer_classes": len(layer_classes),
            "block_classes": len(block_classes),
            "block_kind_counts": dict(sorted(block_kind_counts.items())),
        },
        "shape_coverage": {
            "step_classes_with_shape": sum(1 for v in step_classes.values() if not v.get("has_unknown_shape")),
            "step_classes_total": len(step_classes),
            "layer_classes_with_shape": sum(1 for v in layer_classes.values() if not v.get("has_unknown_shape")),
            "layer_classes_total": len(layer_classes),
            "block_classes_with_shape": sum(1 for v in block_classes.values() if not v.get("has_unknown_shape")),
            "block_classes_total": len(block_classes),
        },
    }
    write_json(output_dir / "classify_manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = classify_profile(Path(args.output))
    emit_stage_json({"stage": "classify", "counts": manifest["counts"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
