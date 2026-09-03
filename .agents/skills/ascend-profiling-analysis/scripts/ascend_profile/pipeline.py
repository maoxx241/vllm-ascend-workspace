#!/usr/bin/env python3
"""CANN pipeline-stage metrics and op-type / bound-class classification.

Column names follow the CANN msprof / kernel_details.csv nomenclature; the
decoupled AIC (cube) / AIV (vector) pipelines are kept strictly separate so
per-stage bottlenecks stay attributable.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

try:
    from .store import pick, try_float
except ImportError:  # pragma: no cover - script-mode fallback
    from store import pick, try_float  # type: ignore[no-redef]


# CANN msprof / kernel_details.csv pipeline column names.  We keep the
# original CANN nomenclature (just drop the "(us)" suffix to make valid
# Python identifiers) so every aggregated metric stays one-to-one with the
# raw evidence column.  No fabrication: if a column is missing the value
# stays out of the dict.
#
# IMPORTANT: AI Core (cube) and AI Vector use a decoupled pipeline on
# Atlas A2/A3, so ``aic_mte2_time`` (GM/L1 -> L0A/L0B for the matmul/cube
# unit) and ``aiv_mte2_time`` (GM -> UB for the vector unit) MUST stay
# separate -- merging them masks the actual bottleneck.
_PIPELINE_SOURCE_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("aicore_time",      ("aicore_time(us)", "aicore_time", "AI Core Time(us)")),
    ("aiv_time",         ("aiv_time(us)", "aiv_time", "AI Vector Time(us)")),
    ("aic_mac_time",     ("aic_mac_time(us)", "aic_mac_time")),
    ("aic_fixpipe_time", ("aic_fixpipe_time(us)", "aic_fixpipe_time")),
    ("aic_mte1_time",    ("aic_mte1_time(us)", "aic_mte1_time")),
    ("aic_mte2_time",    ("aic_mte2_time(us)", "aic_mte2_time")),
    ("aic_scalar_time",  ("aic_scalar_time(us)", "aic_scalar_time")),
    ("aiv_vec_time",     ("aiv_vec_time(us)", "aiv_vec_time")),
    ("aiv_mte2_time",    ("aiv_mte2_time(us)", "aiv_mte2_time")),
    ("aiv_mte3_time",    ("aiv_mte3_time(us)", "aiv_mte3_time")),
    ("aiv_scalar_time",  ("aiv_scalar_time(us)", "aiv_scalar_time")),
)


# The full pipeline schema downstream stages are allowed to assume.  Order
# matters for column layout in operator_summary.csv; CANN convention is
# AIC stages first, AIV stages second.
PIPELINE_FIELDS: tuple[str, ...] = tuple(key for key, _ in _PIPELINE_SOURCE_COLUMNS)


# Stage groups for bound-class derivation.  ``aicore_time`` and
# ``aiv_time`` are totals and intentionally NOT in any group -- they're
# just the per-core wall time.
_AIC_STAGES: tuple[str, ...] = ("aic_mac_time", "aic_fixpipe_time", "aic_mte1_time", "aic_mte2_time", "aic_scalar_time")
_AIV_STAGES: tuple[str, ...] = ("aiv_vec_time", "aiv_mte2_time", "aiv_mte3_time", "aiv_scalar_time")
_PIPELINE_STAGES: tuple[str, ...] = _AIC_STAGES + _AIV_STAGES


_BOUND_FAMILY_BY_STAGE: dict[str, str] = {
    "aic_mac_time":     "cube",
    "aic_fixpipe_time": "cube",
    "aic_mte1_time":    "aic_mte",
    "aic_mte2_time":    "aic_mte",
    "aic_scalar_time":  "scalar",
    "aiv_vec_time":     "vector",
    "aiv_mte2_time":    "aiv_mte",
    "aiv_mte3_time":    "aiv_mte",
    "aiv_scalar_time":  "scalar",
}


def pipeline_breakdown_from_row(row: Mapping[str, Any]) -> dict[str, float]:
    """Extract per-event pipeline times from a kernel_details.csv row.

    Returns an empty dict when no source column is present, so callers can
    detect missing-data cases without fabricating zeros.  Otherwise the
    returned dict maps each ``PIPELINE_FIELDS`` key to a float in
    microseconds.
    """

    out: dict[str, float] = {}
    for key, candidates in _PIPELINE_SOURCE_COLUMNS:
        text = pick(row, candidates, "")
        if not text:
            continue
        out[key] = round(max(0.0, try_float(text)), 6)
    return out


def has_pipeline_signal(pipeline: Mapping[str, Any] | None) -> bool:
    """Return True iff the pipeline dict carries any non-zero stage value.

    The two ``*_time`` totals (``aicore_time`` / ``aiv_time``) are
    excluded -- on a true zero-compute event we may still have a non-zero
    total, so the stage breakdown is the authoritative signal.
    """

    if not pipeline:
        return False
    for key in _PIPELINE_STAGES:
        if float(pipeline.get(key) or 0.0) > 0.0:
            return True
    return False


def sum_pipeline_breakdown(pipelines: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Sum a sequence of pipeline dicts using the original CANN field names.

    Returns an empty dict if no input dict carries any stage signal, so
    callers can preserve ``unknown`` semantics downstream.
    """

    totals: dict[str, float] = {key: 0.0 for key in PIPELINE_FIELDS}
    seen = False
    for pipeline in pipelines:
        if not pipeline:
            continue
        for key in PIPELINE_FIELDS:
            value = pipeline.get(key)
            if value is None:
                continue
            totals[key] += float(value)
        if has_pipeline_signal(pipeline):
            seen = True
    if not seen:
        return {}
    return {key: round(value, 6) for key, value in totals.items()}


_OP_TYPE_BY_CORE: dict[str, str] = {
    "AI_CORE":         "aic",
    "AICORE":          "aic",
    "AI_VECTOR_CORE":  "aiv",
    "AIVECTOR":        "aiv",
    "AI_VECTORCORE":   "aiv",
    "MIX_AIC":         "mix_cv",
    "MIX_AIV":         "mix_cv",
    "MIX_AICAIV":      "mix_cv",
    "MIX_AIC_AIV":     "mix_cv",
    "COMMUNICATION":   "communication",
    "AI_CPU":          "aicpu",
    "AICPU":           "aicpu",
    "DSA_SQE":         "dsa",
}


def op_type_from_event(
    accelerator_core: str | None,
    pipeline: Mapping[str, Any] | None = None,
) -> str:
    """Classify an event into the canonical op_type taxonomy.

    The ``Accelerator Core`` column is the source of truth for whether an
    op runs on AIC, AIV, or both.  We only fall back to pipeline signal
    when the column is absent or unrecognised.

    Special case: a ``COMMUNICATION`` core with non-zero AIV stage time
    indicates a fused comm + AIV kernel (e.g. ``DispatchFFNCombine``,
    ``MoeDistributeDispatch``, ``MoeDistributeCombine``).  Those need a
    distinct label so the report can analyse the AIV burden separately
    from the pure HCCL portion.
    """

    core = (accelerator_core or "").strip().upper()
    base = _OP_TYPE_BY_CORE.get(core)
    if base is None:
        if not core:
            return "unknown"
        if "MIX" in core:
            return "mix_cv"
        if "COMM" in core or "HCCL" in core:
            return "communication"
        if "VECTOR" in core or core.endswith("_AIV"):
            return "aiv"
        if "CORE" in core:
            return "aic"
        return "unknown"

    if base == "communication":
        # Detect fused comm + AIV (dispatch / combine / distribute style ops).
        aiv_signal = 0.0
        if pipeline:
            aiv_signal = float(pipeline.get("aiv_time") or 0.0)
            if aiv_signal <= 0.0:
                for key in _AIV_STAGES:
                    aiv_signal += float(pipeline.get(key) or 0.0)
        if aiv_signal > 0.0:
            return "mix_comm_aiv"
    return base


def bound_class_from_pipeline(
    pipeline: Mapping[str, Any] | None,
    *,
    op_type: str | None = None,
    is_aicpu: bool = False,
    is_communication: bool = False,
    mixed_margin: float = 0.10,
) -> dict[str, Any]:
    """Classify an op-level pipeline aggregate.

    Returns a dict with four keys:
      * ``bound_stage`` -- the single stage (or short-circuit label) with
        the largest cumulative time.  For ``mix_comm_aiv`` only the AIV
        stages are considered (the AIC side of a comm-fused op is not
        meaningful work).
      * ``bound_family`` -- coarser bucket in
        ``{cube, vector, aic_mte, aiv_mte, scalar, mixed, aicpu,
           communication, comm_aiv_mix, dsa, unknown}``.
        ``mixed`` means the top stage is within ``mixed_margin`` of the
        runner-up's family share.  ``comm_aiv_mix`` is a hard-set label
        so the report can group dispatch/combine kernels together.
      * ``dominant_core`` -- ``aic`` / ``aiv`` / ``mix`` / ``none`` based
        on stage-time totals.  ``mix`` for any op_type ``mix_cv`` whose
        stages cover both AIC and AIV with comparable weight.
      * ``op_type`` -- echoed back for convenience so callers don't have
        to thread it separately.

    The decoupled-architecture rule is enforced by deriving the family
    from per-stage time, never from a merged compute-vs-MTE ratio: AIC
    mte2 stalls and AIV mte2 stalls land in different families
    (``aic_mte`` vs ``aiv_mte``) so the report can call them out
    separately.
    """

    op_type_resolved = op_type or "unknown"

    if is_aicpu or op_type_resolved == "aicpu":
        return {"bound_stage": "aicpu", "bound_family": "aicpu", "dominant_core": "none", "op_type": "aicpu"}
    if op_type_resolved == "dsa":
        return {"bound_stage": "dsa", "bound_family": "dsa", "dominant_core": "none", "op_type": "dsa"}
    if op_type_resolved == "communication" or (is_communication and op_type_resolved not in {"mix_comm_aiv"}):
        return {"bound_stage": "communication", "bound_family": "communication", "dominant_core": "none", "op_type": "communication"}

    pipeline = pipeline or {}

    if op_type_resolved == "mix_comm_aiv":
        aiv_us = {key: float(pipeline.get(key) or 0.0) for key in _AIV_STAGES}
        if sum(aiv_us.values()) <= 0:
            return {"bound_stage": "communication", "bound_family": "comm_aiv_mix", "dominant_core": "none", "op_type": "mix_comm_aiv"}
        bound_stage = max(aiv_us, key=aiv_us.get)
        return {"bound_stage": bound_stage, "bound_family": "comm_aiv_mix", "dominant_core": "aiv", "op_type": "mix_comm_aiv"}

    if not has_pipeline_signal(pipeline):
        return {"bound_stage": "unknown", "bound_family": "unknown", "dominant_core": "none", "op_type": op_type_resolved}

    stage_us: dict[str, float] = {key: float(pipeline.get(key) or 0.0) for key in _PIPELINE_STAGES}
    total = sum(stage_us.values())
    if total <= 0:
        return {"bound_stage": "unknown", "bound_family": "unknown", "dominant_core": "none", "op_type": op_type_resolved}

    bound_stage = max(stage_us, key=stage_us.get)
    family_total: dict[str, float] = {}
    for stage, value in stage_us.items():
        family_total[_BOUND_FAMILY_BY_STAGE[stage]] = family_total.get(_BOUND_FAMILY_BY_STAGE[stage], 0.0) + value
    sorted_families = sorted(family_total.items(), key=lambda item: item[1], reverse=True)
    top_family, top_value = sorted_families[0]
    runner_value = sorted_families[1][1] if len(sorted_families) > 1 else 0.0
    if total > 0 and (top_value - runner_value) / total < mixed_margin:
        bound_family = "mixed"
    else:
        bound_family = top_family

    aic_total = sum(stage_us[key] for key in _AIC_STAGES)
    aiv_total = sum(stage_us[key] for key in _AIV_STAGES)
    if aic_total <= 0 and aiv_total <= 0:
        dominant_core = "none"
    elif aic_total <= 0:
        dominant_core = "aiv"
    elif aiv_total <= 0:
        dominant_core = "aic"
    elif abs(aic_total - aiv_total) / max(aic_total, aiv_total) < mixed_margin:
        dominant_core = "mix"
    elif aic_total > aiv_total:
        dominant_core = "aic"
    else:
        dominant_core = "aiv"

    return {"bound_stage": bound_stage, "bound_family": bound_family, "dominant_core": dominant_core, "op_type": op_type_resolved}
