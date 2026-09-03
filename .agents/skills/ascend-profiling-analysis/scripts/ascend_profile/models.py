#!/usr/bin/env python3
"""Schema dataclasses for the Ascend profiling analysis framework.

Pure data-model module: no internal imports, no IO. Everything here is a
frozen dataclass so stage outputs stay hashable and diff-friendly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    kind: str
    path: str
    sha256: str | None = None
    rank_id: str | None = None
    row_base: str = "zero_based"
    row_start: int | None = None
    row_end: int | None = None


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    profile_id: str
    rank_id: str
    source_id: str
    row_idx: int
    name_raw: str
    task_type: str
    accelerator_core: str
    stream_id: str
    start_us: float
    end_us: float
    duration_us: float
    wait_us: float
    op_categories: tuple[str, ...] = ()
    op_roles: tuple[str, ...] = ()
    shape_signature: str | None = None
    shape_features: dict[str, Any] = field(default_factory=dict)
    raw_fields_ref: SourceRef | None = None
    # Per-event pipeline breakdown read directly from kernel_details.csv
    # extended columns. All values are absolute microseconds within the
    # event's duration. Empty dict when the source CSV does not expose the
    # corresponding columns (older CANN profilers); we never fabricate.
    pipeline_us: dict[str, float] = field(default_factory=dict)
    # Canonical op type derived from the ``Accelerator Core`` column plus
    # AIV-signal heuristic. Range:
    # ``aic | aiv | mix_cv | mix_comm_aiv | communication | aicpu | dsa | unknown``.
    # See ``op_type_from_event`` for the rules.
    op_type: str = "unknown"


@dataclass(frozen=True)
class StepSegment:
    segment_id: str
    rank_id: str
    segment_type: str
    complete: bool
    row_start: int
    row_end: int
    start_us: float
    end_us: float
    cluster_id: str | None = None
    step_family: str | None = None
    main_layer_count: int | None = None
    speculative_layer_count: int | None = None
    structure_signature: str | None = None
    layer_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayerSegment:
    layer_id: str
    rank_id: str
    segment_id: str
    layer_index: int
    layer_role: str
    boundary_source: str
    row_start: int
    row_end: int
    start_us: float
    end_us: float
    structure_signature: str | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlockSegment:
    """Sub-layer block: ``attention``, ``ffn``, or ``moe``.

    A vLLM transformer layer typically decomposes into one ``attention``
    block followed by one ``ffn`` (dense FFN) or ``moe`` block.  Layers
    without any attention kernel are flagged as ``companion_layer`` -- e.g.
    the eager-mode bookkeeping passes that run alongside a graph-mode
    forward, or sampling-only layers in the speculative head.

    The block boundary is derived strictly from event roles inside the
    parent ``LayerSegment``, never from a name heuristic, so the
    decomposition stays evidence-grade.
    """

    block_id: str
    rank_id: str
    segment_id: str
    layer_id: str
    layer_index: int
    block_index: int
    block_kind: str
    companion_layer: bool
    row_start: int
    row_end: int
    start_us: float
    end_us: float
    event_count: int = 0
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructureObservation:
    structure_id: str
    scope_type: str
    rank_id: str
    role: str
    role_family: str
    confidence: str
    segment_id: str | None = None
    layer_id: str | None = None
    implementation_evidence: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    kind: str
    summary: str
    source_refs: tuple[SourceRef, ...] = ()
    event_ids: tuple[str, ...] = ()
    segment_ids: tuple[str, ...] = ()
    layer_ids: tuple[str, ...] = ()
    alignment_ids: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrossRankAlignment:
    alignment_id: str
    alignment_type: str
    rank_ids: tuple[str, ...]
    segment_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    start_us: float | None = None
    end_us: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosisFinding:
    claim_id: str
    claim_type: str
    summary: str
    confidence: str
    finding_type: str
    scope: str
    severity: str = "info"
    rank_ids: tuple[str, ...] = ()
    alignment_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Interval:
    start_us: float
    end_us: float

    @property
    def duration_us(self) -> float:
        return max(0.0, self.end_us - self.start_us)


@dataclass(frozen=True)
class BusySegment:
    start_us: float
    end_us: float
    first_event: NormalizedEvent
    last_event: NormalizedEvent

    @property
    def duration_us(self) -> float:
        return max(0.0, self.end_us - self.start_us)
