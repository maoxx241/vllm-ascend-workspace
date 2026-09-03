#!/usr/bin/env python3
"""Backwards-compatible facade over the split framework modules.

The former god-module was split by responsibility:

* ``models``   — schema dataclasses (SourceRef, NormalizedEvent, segments, …)
* ``store``    — artifact IO (JSON / JSONL / CSV / XLSX), id/time helpers,
  and the shared scalar coercion helpers (``to_float`` / ``to_int`` /
  ``norm_text`` / ``first_present`` / ``text_config`` / ``parse_jsonish``)
* ``sources``  — rank-dir discovery and kernel_details.csv row extraction
* ``pipeline`` — CANN pipeline-stage metrics, op_type and bound-class rules
* ``work``     — best-effort FLOP / byte estimates from shape fields
* ``rules``    — kernel taxonomy (``categories_and_roles``) and the
  attention-family resolver
* ``metrics``  — event-set analytics (interval union, bubbles) and the
  normalized-artifact loaders

This module re-exports the full historic public surface so existing
consumers (stage modules, tests) keep working unchanged. **New code should
import from the real modules above, not from this facade.**
"""

from __future__ import annotations

try:
    from .metrics import (
        bubble_windows,
        evidence_event,
        group_by_rank,
        load_block_segments,
        load_events,
        load_events_csv,
        load_layer_segments,
        load_step_segments,
        merge_event_segments,
        metrics_for_events,
        quantile,
        row_ranges,
        select_events,
    )
    from .models import (
        BlockSegment,
        BusySegment,
        CrossRankAlignment,
        DiagnosisFinding,
        EvidenceRef,
        Interval,
        LayerSegment,
        NormalizedEvent,
        SourceRef,
        StepSegment,
        StructureObservation,
    )
    from .pipeline import (
        PIPELINE_FIELDS,
        _OP_TYPE_BY_CORE,
        bound_class_from_pipeline,
        has_pipeline_signal,
        op_type_from_event,
        pipeline_breakdown_from_row,
        sum_pipeline_breakdown,
    )
    from .rules import (
        categories_and_roles,
        is_ai_core_like,
        is_aicpu_event,
        is_comm_event,
        refine_dense_attention_from_shapes,
        resolve_attention_family,
    )
    from .sources import (
        SHAPE_SIGNATURE_DIM_SAMPLE_LIMIT,
        core_from_row,
        discover_rank_dirs,
        event_time_from_row,
        infer_rank_id,
        kernel_details_path,
        name_from_row,
        shape_signature,
        stream_from_row,
        supplemental_sources,
        task_type_from_row,
    )
    from .store import (
        KNOWLEDGE_DIR,
        SCHEMA_VERSION,
        SPREADSHEET_COLUMN_BASE,
        TOOL_VERSION,
        column_name,
        csv_rows,
        csv_value,
        emit_stage_json,
        first_present,
        fold_text,
        iter_csv_rows,
        norm_text,
        parse_jsonish,
        pick,
        read_csv,
        read_json,
        read_jsonl,
        row_key_lookup,
        safe_sheet_name,
        sha256_file,
        sheet_xml,
        stable_id,
        text_config,
        to_float,
        to_int,
        to_plain,
        try_float,
        utc_now,
        write_csv,
        write_json,
        write_jsonl,
        write_xlsx,
    )
    from .work import (
        dtype_bytes,
        estimate_attention_flops,
        estimate_matmul_flops,
        estimate_vector_flops,
        estimated_work_from_row,
        parse_tensor_dtypes,
        parse_tensor_shapes,
        tensor_list_bytes,
        tensor_numel,
    )
except ImportError:  # pragma: no cover - script-mode fallback
    from metrics import (  # type: ignore[no-redef]
        bubble_windows,
        evidence_event,
        group_by_rank,
        load_block_segments,
        load_events,
        load_events_csv,
        load_layer_segments,
        load_step_segments,
        merge_event_segments,
        metrics_for_events,
        quantile,
        row_ranges,
        select_events,
    )
    from models import (  # type: ignore[no-redef]
        BlockSegment,
        BusySegment,
        CrossRankAlignment,
        DiagnosisFinding,
        EvidenceRef,
        Interval,
        LayerSegment,
        NormalizedEvent,
        SourceRef,
        StepSegment,
        StructureObservation,
    )
    from pipeline import (  # type: ignore[no-redef]
        PIPELINE_FIELDS,
        _OP_TYPE_BY_CORE,
        bound_class_from_pipeline,
        has_pipeline_signal,
        op_type_from_event,
        pipeline_breakdown_from_row,
        sum_pipeline_breakdown,
    )
    from rules import (  # type: ignore[no-redef]
        categories_and_roles,
        is_ai_core_like,
        is_aicpu_event,
        is_comm_event,
        refine_dense_attention_from_shapes,
        resolve_attention_family,
    )
    from sources import (  # type: ignore[no-redef]
        SHAPE_SIGNATURE_DIM_SAMPLE_LIMIT,
        core_from_row,
        discover_rank_dirs,
        event_time_from_row,
        infer_rank_id,
        kernel_details_path,
        name_from_row,
        shape_signature,
        stream_from_row,
        supplemental_sources,
        task_type_from_row,
    )
    from store import (  # type: ignore[no-redef]
        KNOWLEDGE_DIR,
        SCHEMA_VERSION,
        SPREADSHEET_COLUMN_BASE,
        TOOL_VERSION,
        column_name,
        csv_rows,
        csv_value,
        emit_stage_json,
        first_present,
        fold_text,
        iter_csv_rows,
        norm_text,
        parse_jsonish,
        pick,
        read_csv,
        read_json,
        read_jsonl,
        row_key_lookup,
        safe_sheet_name,
        sha256_file,
        sheet_xml,
        stable_id,
        text_config,
        to_float,
        to_int,
        to_plain,
        try_float,
        utc_now,
        write_csv,
        write_json,
        write_jsonl,
        write_xlsx,
    )
    from work import (  # type: ignore[no-redef]
        dtype_bytes,
        estimate_attention_flops,
        estimate_matmul_flops,
        estimate_vector_flops,
        estimated_work_from_row,
        parse_tensor_dtypes,
        parse_tensor_shapes,
        tensor_list_bytes,
        tensor_numel,
    )
