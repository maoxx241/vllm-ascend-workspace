"""Regression tests for segment-stage exact-cover validation.

These tests pin the two contracts that surfaced when sweeping DSV4 /
nextprof profiles:

1. ``validate_unresolved_composite_bodies`` must accept a multi-layer
   plan whose own sequence is itself a recurring template (>=2
   occurrences in the same rank). Demanding that such a plan also be
   coverable by strictly-smaller templates produced false positives on
   DSV4 prefill (30+ ``[combine, gating, dispatch, expert_matmul x2,
   combine]`` plans per rank) and on a long nextprof MoE-only run.

2. ``classify_residual_plans`` must tag an interior 1-layer template
   residual as ``partial_body_window`` when its layer's boundary key
   already appears within some recurring template's layer sequence.
   Tagging it ``unclassified_island`` causes
   ``validate_exact_cover`` to raise ``interior_template_residual``
   on DSV4 decode / 0420 prefill ranks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _SKILL_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ascend_profile import segment as segm  # noqa: E402
from ascend_profile.segment import (  # noqa: E402
    LayerFrame,
    LayerObservation,
    build_layers,
    StepPlan,
    classify_interior_substructure_plans,
    classify_residual_plans,
    compose_step_plans,
    event_role,
    validate_exact_cover,
    validate_unresolved_composite_bodies,
)
from ascend_profile.common import NormalizedEvent  # noqa: E402


def _layer(index: int, signature: str, *, row_base: int = 0) -> LayerObservation:
    """Build a minimal ``LayerObservation`` with the requested signature.

    The validator only reads ``signature``, ``regime_key``, ``row_start``,
    ``row_end`` and ``anchors``; ``anchors`` is unused by the routines
    under test, so we leave it empty.
    """

    row_start = row_base + index * 10
    return LayerObservation(
        index=index,
        row_start=row_start,
        row_end=row_start + 9,
        anchors=(),
        signature=signature,
        regime_key=segm.layer_regime_key(signature),
    )


def _step_plan(
    signatures: Iterable[str],
    *,
    segment_type: str = "step",
    complete: bool = True,
    tags: tuple[str, ...] = (),
    row_base: int = 0,
) -> StepPlan:
    layers = tuple(_layer(i, sig, row_base=row_base) for i, sig in enumerate(signatures))
    frame = LayerFrame(layers=layers, reason="test", tags=tags)
    return StepPlan(
        frames=(frame,),
        main_frame_count=1,
        reason="test",
        segment_type=segment_type,
        complete=complete,
    )


def _event(
    row_idx: int,
    name: str,
    *,
    categories: tuple[str, ...] = (),
    roles: tuple[str, ...] = (),
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"evt_{row_idx}",
        profile_id="profile_test",
        rank_id="rank0",
        source_id="src_test",
        row_idx=row_idx,
        name_raw=name,
        task_type=name.upper(),
        accelerator_core="AI_CORE",
        stream_id="0",
        start_us=float(row_idx),
        end_us=float(row_idx) + 0.5,
        duration_us=0.5,
        wait_us=0.0,
        op_categories=categories,
        op_roles=roles,
    )


def _build_test_layers(events: list[NormalizedEvent]) -> list[LayerObservation]:
    row_numbers = tuple(event.row_idx for event in events)
    boundary_rows = segm.dedup_adjacent_event_rows(
        events,
        lambda event: event_role(event, "block_head") or event_role(event, "normalization"),
    )
    anchor_boundary_rows = segm.dedup_adjacent_event_rows(
        events,
        lambda event: event_role(event, "block_head"),
    )
    return build_layers(events, row_numbers, boundary_rows, anchor_boundary_rows, ())


def test_mla_sparse_attention_subunits_stay_in_one_layer() -> None:
    """GLM5-style MLA decode exposes several attention-labelled subunits.

    Only the layer-start MLA marker should define layer frequency.  The sparse
    score and V-up projection are internal attention evidence for the same
    model layer, otherwise one transformer layer is reported as three layers.
    """

    events = [
        _event(0, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)),
        _event(1, "MlaPrologV3", categories=("attention.mla", "attention.mla.preprocess"), roles=("attention",)),
        _event(2, "KvQuantSparseFlashAttention", categories=("attention.flash_score",), roles=("attention",)),
        _event(
            3,
            "aclnnTransposeBatchMatMul",
            categories=("attention.mla.v_up_proj", "attention.sparse_attn.v_up_proj", "compute.matmul"),
            roles=("attention", "compute"),
        ),
        _event(4, "MoeDistributeDispatchV3", categories=("moe.dispatch",), roles=("moe",)),
        _event(5, "MoeDistributeCombineV3", categories=("moe.combine",), roles=("moe",)),
        _event(6, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)),
        _event(7, "MlaPrologV3", categories=("attention.mla", "attention.mla.preprocess"), roles=("attention",)),
        _event(8, "KvQuantSparseFlashAttention", categories=("attention.flash_score",), roles=("attention",)),
        _event(
            9,
            "aclnnTransposeBatchMatMul",
            categories=("attention.mla.v_up_proj", "attention.sparse_attn.v_up_proj", "compute.matmul"),
            roles=("attention", "compute"),
        ),
    ]

    layers = _build_test_layers(events)

    assert len(layers) == 2
    assert [tuple(anchor.name_raw for anchor in layer.anchors) for layer in layers] == [
        ("MlaPrologV3",),
        ("MlaPrologV3",),
    ]
    assert layers[0].row_start == 0
    assert layers[0].row_end == 5
    assert "attention.flash_scorex1" in layers[0].signature
    assert "attention.mla.v_up_projx1" in layers[0].signature
    assert "moe.dispatchx1" in layers[0].signature


def test_flash_attention_still_anchors_when_no_mla_marker() -> None:
    """Dense attention profiles without MLA markers still use flash score as
    the layer-frequency anchor.
    """

    events = [
        _event(0, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)),
        _event(1, "FusedInferAttentionScore", categories=("attention.flash_score",), roles=("attention",)),
        _event(2, "aclnnMatmul", categories=("compute.matmul",), roles=("compute",)),
        _event(3, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)),
        _event(4, "FusedInferAttentionScore", categories=("attention.flash_score",), roles=("attention",)),
        _event(5, "aclnnMatmul", categories=("compute.matmul",), roles=("compute",)),
    ]

    layers = _build_test_layers(events)

    assert len(layers) == 2
    assert [tuple(anchor.name_raw for anchor in layer.anchors) for layer in layers] == [
        ("FusedInferAttentionScore",),
        ("FusedInferAttentionScore",),
    ]


_GLM_MLA_ATTENTION = (
    "attention:attention.flash_scorex1+attention.lightning_indexerx1+"
    "attention.mlax1+attention.mla.preprocessx1+attention.mla.v_up_projx1+"
    "attention.ropex2+attention.sparse_attn.v_up_projx1"
)
_GLM_DENSE_LAYER = f"{_GLM_MLA_ATTENTION}|ffn_or_dense_compute|block_head"
_GLM_MOE_LAYER = (
    f"{_GLM_MLA_ATTENTION}|"
    "moe:moe.combinex1+moe.dispatchx1+moe.expert_matmulx2+moe.gatingx1|block_head"
)


def test_glm_dense_prefix_stays_in_main_body_before_mtp_tail() -> None:
    """GLM5 has dense early main layers followed by MoE main layers.

    That dense->MoE transition is a model-layer family transition inside the
    main body.  It must not split the 78-layer body into a fake 3-layer step
    followed by a 75-layer step with a 3-layer MTP tail.
    """

    main_layers = tuple(
        _layer(index, _GLM_DENSE_LAYER if index < 3 else _GLM_MOE_LAYER)
        for index in range(78)
    )
    main_frame = LayerFrame(
        layers=main_layers,
        reason="selection_delimited_body",
        selection_after=(1000,),
    )

    assert segm.split_frame_by_regime(main_frame) == [main_frame]

    mtp_frames = tuple(
        LayerFrame(
            layers=(_layer(78 + offset, _GLM_MOE_LAYER),),
            reason="selection_delimited_body",
            selection_before=(1000 + offset,),
            selection_after=(1001 + offset,),
        )
        for offset in range(3)
    )

    plans = compose_step_plans((main_frame, *mtp_frames))

    assert len(plans) == 1
    assert len(plans[0].main_layers) == 78
    assert len(plans[0].all_layers) == 81
    assert plans[0].reason == "main_body+selection_delimited_speculative_tail"


# ---------------------------------------------------------------------------
# Fix B: a plan whose own sequence is recurring must not be flagged
# ---------------------------------------------------------------------------

_DSV4_FUSED_SEQ = (
    "block_head|moe:moe.combinex1",
    "moe:moe.gatingx1|normalization_no_visible_block_head",
    "moe:moe.dispatchx1",
    "moe:moe.expert_matmulx1",
    "moe:moe.expert_matmulx1",
    "block_head|moe:moe.combinex1",
)

_DSV4_PURE_MOE_SEQ = (
    "moe:moe.dispatchx1",
    "moe:moe.expert_matmulx1",
    "moe:moe.expert_matmulx1",
)


def test_recurring_sequence_is_accepted_even_with_embedded_template() -> None:
    """The 6-layer ``[combine, gating, dispatch, expert x2, combine]`` body
    repeats 5 times in this rank.  It embeds a strictly smaller 3-layer
    ``[dispatch, expert x2]`` template that repeats 4 times.  Pre-fix the
    validator failed because it could only consider strictly smaller
    templates; post-fix the recurrence of the body itself is taken as
    direct evidence and no hard error is raised.
    """

    plans = [
        _step_plan(_DSV4_FUSED_SEQ, row_base=0),
        _step_plan(_DSV4_PURE_MOE_SEQ, row_base=100),
        _step_plan(_DSV4_FUSED_SEQ, row_base=200),
        _step_plan(_DSV4_PURE_MOE_SEQ, row_base=300),
        _step_plan(_DSV4_FUSED_SEQ, row_base=400),
        _step_plan(_DSV4_PURE_MOE_SEQ, row_base=500),
        _step_plan(_DSV4_FUSED_SEQ, row_base=600),
        _step_plan(_DSV4_PURE_MOE_SEQ, row_base=700),
        _step_plan(_DSV4_FUSED_SEQ, row_base=800),
    ]
    errors = validate_unresolved_composite_bodies("rank0", plans)
    assert errors == [], (
        "validator must accept a body whose own sequence is itself a "
        f"recurring template; got {errors!r}"
    )


def test_unique_composite_body_still_flagged() -> None:
    """A multi-layer body that contains an embedded smaller template
    but appears only once must still be flagged, otherwise we lose the
    safety net that catches genuinely fused / merged steps.
    """

    unique_long_seq = (
        "block_head|moe:moe.gatingx1",
        "moe:moe.dispatchx1",
        "moe:moe.expert_matmulx1",
        "moe:moe.expert_matmulx1",
        "block_head|moe:moe.unusual_tail",
    )
    plans = [
        _step_plan(_DSV4_PURE_MOE_SEQ, row_base=0),
        _step_plan(_DSV4_PURE_MOE_SEQ, row_base=100),
        _step_plan(_DSV4_PURE_MOE_SEQ, row_base=200),
        _step_plan(unique_long_seq, row_base=300),  # appears only once
    ]
    errors = validate_unresolved_composite_bodies("rank0", plans)
    assert len(errors) == 1
    assert errors[0]["error_type"] == "unresolved_composite_body"
    assert errors[0]["current_layers"] == len(unique_long_seq)


# ---------------------------------------------------------------------------
# Fix C: interior 1-layer template residuals -> partial_body_window
# ---------------------------------------------------------------------------

_RECURRING_LAYER_SIGS_FOR_RESIDUAL = (
    "attention:attention.rope.partialx1+attention.ropex1|block_head|moe:moe.combinex1",
    "attention:attention.kv_compressorx2+attention.rope.partialx2+attention.ropex2+attention.sparse_sharedkvx1|normalization_no_visible_block_head",
    "attention:attention.mla.v_up_projx1+attention.sparse_attn.v_up_projx1|block_head|moe:moe.gatingx1",
)


def _residual_plan(signature: str, row_base: int) -> StepPlan:
    """A 1-layer plan flagged as a template-prefix residual by upstream
    composer stages.  ``classify_residual_plans`` reads the
    ``exact_template_prefix_residual`` tag to detect template residuals.
    """

    layer = _layer(0, signature, row_base=row_base)
    frame = LayerFrame(
        layers=(layer,),
        reason="residual",
        tags=("exact_template_prefix_residual",),
    )
    return StepPlan(
        frames=(frame,),
        main_frame_count=1,
        reason="residual",
        segment_type="step",
        complete=True,
    )


def test_interior_residual_with_known_layer_role_becomes_partial_body_window() -> None:
    """A leftover 1-layer ``[block_head|moe.combine]`` between two proven
    3-layer attention bodies sits squarely inside one of the recurring
    template's layer roles, so it must be classified as
    ``partial_body_window`` (allowed by ``validate_exact_cover``) rather
    than ``unclassified_island`` (which raises
    ``interior_template_residual``).
    """

    plans = [
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=0),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=100),
        _residual_plan(
            "block_head|moe:moe.combinex1",
            row_base=200,
        ),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=300),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=400),
    ]
    classified = classify_residual_plans(plans)
    # 4 complete plans + 1 residual
    assert len(classified) == 5
    residual = classified[2]
    assert residual.segment_type == "partial_body_window", (
        "interior residual whose boundary key appears in a recurring "
        "template's layer sequence must be partial_body_window, "
        f"got {residual.segment_type!r}"
    )


def test_interior_residual_with_unknown_role_remains_unclassified_island() -> None:
    """An interior residual that occurs only once AND whose role does
    not appear in any recurring template's layer sequence must stay
    ``unclassified_island`` — we never silently absorb truly novel
    one-off fragments, otherwise real anomalies would be hidden.
    """

    plans = [
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=0),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=100),
        _residual_plan(
            "attention:attention.never_seen_anywherex1|block_head|moe:moe.never_seenx1",
            row_base=200,
        ),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=300),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=400),
    ]
    classified = classify_residual_plans(plans)
    residual = classified[2]
    assert residual.segment_type == "unclassified_island"


def test_recurring_residual_with_superset_role_becomes_partial_body_window() -> None:
    """A residual whose role is *richer* than any recurring template
    layer (so the subset check fails) must still be classified as
    ``partial_body_window`` when its own core-role sequence recurs
    >= 2 times among the template residuals.

    DSV4 0420 prefill emits 22 identical 1-layer fragments of the form
    ``[lightning_indexer + rope + dispatch_expert_compute]``: a TBO
    overlap of attention indexer and MoE dispatch that does not match
    any single layer of the surrounding 128-layer template, but is
    clearly a stable recurring boundary pattern.
    """

    tbo_signature = (
        "attention:attention.lightning_indexerx2+"
        "attention.rope.partialx1+attention.ropex1|"
        "block_head|moe:moe.dispatch_expert_computex1"
    )
    plans = [
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=0),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=100),
        _residual_plan(tbo_signature, row_base=200),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=300),
        _residual_plan(tbo_signature, row_base=400),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=500),
    ]
    classified = classify_residual_plans(plans)
    # Both residuals (at indices 2 and 4) recur identically; both must
    # be promoted to partial_body_window.
    assert classified[2].segment_type == "partial_body_window"
    assert classified[4].segment_type == "partial_body_window"


def test_leading_template_residual_still_becomes_head() -> None:
    plans = [
        _residual_plan(
            "block_head|moe:moe.combinex1",
            row_base=0,
        ),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=100),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=200),
    ]
    classified = classify_residual_plans(plans)
    assert classified[0].segment_type == "head"


def test_trailing_template_residual_still_becomes_tail() -> None:
    plans = [
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=0),
        _step_plan(_RECURRING_LAYER_SIGS_FOR_RESIDUAL, row_base=100),
        _residual_plan(
            "block_head|moe:moe.combinex1",
            row_base=200,
        ),
    ]
    classified = classify_residual_plans(plans)
    assert classified[-1].segment_type == "tail"


def test_no_interior_template_residual_after_substructure_classification() -> None:
    """End-to-end Fix B regression: once a residual interior plan has been
    promoted to ``partial_body_window`` (Fix C path) or ``runner_window``
    (Fix A path), the downstream ``validate_exact_cover`` step MUST NOT
    emit an ``interior_template_residual`` hard error for it.

    This was the failure mode that broke the 350TPS A3 DSV4 sweep on
    tp1/tp4/tp7 ranks: residuals were being correctly tagged by the
    classifier but the validator's allow-list missed ``runner_window``
    / ``partial_body_window``, so every interior residual still raised
    the hard error. Pin the allow-list explicitly so future tightening
    of ``validate_exact_cover`` can't regress the contract.
    """

    plans = [
        _attention_body_plan(row_base=0),
        # Substructure tagged by classify_interior_substructure_plans.
        StepPlan(
            frames=_attention_substructure_plan(row_base=100).frames,
            main_frame_count=1,
            reason="substructure",
            segment_type="partial_body_window",
            complete=False,
        ),
        _attention_body_plan(row_base=200),
        # Runner window tagged by classify_interior_substructure_plans.
        StepPlan(
            frames=_no_attention_substructure_plan(row_base=300).frames,
            main_frame_count=1,
            reason="substructure",
            segment_type="runner_window",
            complete=False,
        ),
        _attention_body_plan(row_base=400),
    ]
    errors = validate_exact_cover(
        rank_id="rank0",
        events=(),
        row_numbers=(),
        plans=plans,
    )
    residuals = [
        e for e in errors if e.get("error_type") == "interior_template_residual"
    ]
    assert residuals == [], (
        "validate_exact_cover must accept partial_body_window / "
        f"runner_window plans without raising; got {residuals!r}"
    )


# ---------------------------------------------------------------------------
# Fix A: classify_interior_substructure_plans performance + correctness
# ---------------------------------------------------------------------------
#
# Long single-rank profiles (90k+ events / 5k+ complete plans) used to time
# out in segment because ``is_substructure_of_reference`` recomputed each
# reference frame's boundary/core counters for every candidate.  The fix
# hoists reference-side computations out of the per-plan loop and caches
# the pure-string helpers (``layer_role_key`` / ``core_role_key`` /
# ``layer_boundary_key`` / ``split_role_count`` and the per-signature
# component tuples).  The contract the optimization must preserve:
#   * a smaller candidate whose coarse-core/boundary counter is strictly
#     dominated by some reference body is reclassified to
#     ``partial_body_window``;
#   * a same-length candidate with the same signature is left untouched;
#   * the cached helpers return identical results on repeated calls.


_MLA_BODY_LAYER_SIGS = (
    "attention:attention.mla.kv_compressorx1+attention.mla.q_a_projx1|block_head|moe:moe.combinex1",
    "attention:attention.mla.kv_decompressorx1+attention.mla.k_b_projx1|normalization_no_visible_block_head",
    "attention:attention.mla.v_up_projx1|block_head|moe:moe.gatingx1",
)


def _attention_body_plan(row_base: int) -> StepPlan:
    """A 3-layer body that contains an ``attention:`` term (so
    ``plan_has_primary_attention`` is True and it can serve as a reference
    frame for substructure classification).
    """

    return _step_plan(_MLA_BODY_LAYER_SIGS, row_base=row_base)


def _no_attention_substructure_plan(row_base: int) -> StepPlan:
    """A 1-layer plan whose core role is a strict subset of the reference
    body and that contains no ``attention:`` term.  The substructure
    branch maps this to ``runner_window`` (no primary attention).
    """

    return _step_plan(
        ("block_head|moe:moe.combinex1",),
        row_base=row_base,
    )


def _attention_substructure_plan(row_base: int) -> StepPlan:
    """A 2-layer attention-bearing plan whose coarse core components are
    a strict subset of the 3-layer reference body.  ``plan_has_primary_attention``
    is True, so the substructure branch maps this to ``partial_body_window``.
    """

    return _step_plan(_MLA_BODY_LAYER_SIGS[:2], row_base=row_base)


def test_no_attention_candidate_with_attention_reference_kept_as_step() -> None:
    """A no-attention candidate (e.g. a pure MoE dispatch+expert+combine
    mini-step) sitting between attention-bearing reference bodies must
    NOT be demoted to ``runner_window``.

    The pre-Fix-B behavior assumed every no-attention candidate adjacent
    to an attention body was a fragment of that body; in mixed-traffic
    EP profiles this is false (decode MoE-only forwards happily
    co-reside with a prefill mega-step on the same rank).  Fix B
    suppresses demotion across mismatched workload signatures
    (attention reference vs no-attention candidate) so both keep their
    independent ``step`` classification.
    """

    plans = [
        _attention_body_plan(row_base=0),
        _no_attention_substructure_plan(row_base=100),
        _attention_body_plan(row_base=200),
        _no_attention_substructure_plan(row_base=300),
        _attention_body_plan(row_base=400),
    ]
    classified = classify_interior_substructure_plans(plans)
    assert [plan.segment_type for plan in classified] == [
        "step",
        "step",
        "step",
        "step",
        "step",
    ]
    assert all(plan.complete for plan in classified)


def test_attention_substructure_becomes_partial_body_window() -> None:
    """A 2-layer attention-bearing plan whose coarse core components are
    a strict subset of the 3-layer reference attention body, sitting
    between two complete attention bodies, must be reclassified as
    ``partial_body_window``.  This is the path Fix A's reference-hoisted
    inner loop must preserve.
    """

    plans = [
        _attention_body_plan(row_base=0),
        _attention_substructure_plan(row_base=100),
        _attention_body_plan(row_base=200),
        _attention_substructure_plan(row_base=300),
        _attention_body_plan(row_base=400),
    ]
    classified = classify_interior_substructure_plans(plans)
    assert [plan.segment_type for plan in classified] == [
        "step",
        "partial_body_window",
        "step",
        "partial_body_window",
        "step",
    ]
    assert classified[1].complete is False
    assert classified[3].complete is False


def test_singleton_attention_reference_does_not_absorb_lone_moe_only_bodies() -> None:
    """A rank with one long attention reference body (e.g. a single
    prefill forward) plus many short MoE-only bodies (decode-style
    dispatch+expert+combine mini-steps) must not have those mini-steps
    absorbed as ``runner_window``.

    Pre-fix, ``classify_interior_substructure_plans`` skipped the
    ``surrounded_by_complete_attention`` safety check whenever the
    candidate had no primary attention term.  In a mixed-traffic EP
    profile (dsv4 350TPS) the rank had exactly one prefill mega-step
    plus dozens of decode MoE-only bodies; every decode body was
    counter-dominated by the prefill body and thus silently demoted to
    ``runner_window``.  The lone reference body cannot satisfy
    "surrounded by attention on both sides", so requiring the
    surrounded check uniformly is the right behavior.
    """

    long_attention_body = tuple(
        f"attention:attention.mla.kv_compressorx1+attention.ropex1|block_head|moe:moe.expertx1"
        for _ in range(20)
    )
    prefill_body = _step_plan(long_attention_body, row_base=0)
    moe_only_mini = _step_plan(
        (
            "moe:moe.dispatchx1",
            "moe:moe.expert_matmulx1",
            "moe:moe.combinex1",
        ),
        row_base=10_000,
    )
    second_moe_only_mini = _step_plan(
        (
            "moe:moe.dispatchx1",
            "moe:moe.expert_matmulx1",
            "moe:moe.combinex1",
        ),
        row_base=20_000,
    )

    plans = [prefill_body, moe_only_mini, second_moe_only_mini]
    classified = classify_interior_substructure_plans(plans)
    assert [plan.segment_type for plan in classified] == ["step", "step", "step"]
    assert all(plan.complete for plan in classified)


def test_substructure_same_length_body_is_not_reclassified() -> None:
    """An equal-length body must never be flagged as substructure even if
    its core components happen to match.  Fix A's length filter is the
    first cheap rejection in the inner loop.
    """

    plans = [
        _attention_body_plan(row_base=0),
        _attention_body_plan(row_base=100),
        _attention_body_plan(row_base=200),
    ]
    classified = classify_interior_substructure_plans(plans)
    assert [plan.segment_type for plan in classified] == ["step", "step", "step"]
    assert all(plan.complete for plan in classified)


def test_classify_interior_substructure_plans_is_idempotent() -> None:
    """Running ``classify_interior_substructure_plans`` twice on the same
    input must yield the same output.  This guards against the cached
    helpers leaking mutable state between calls (the optimization moved
    ``coarse_core_components`` behind a cached tuple representation).
    """

    plans = [
        _attention_body_plan(row_base=0),
        _no_attention_substructure_plan(row_base=100),
        _attention_body_plan(row_base=200),
        _no_attention_substructure_plan(row_base=300),
        _attention_body_plan(row_base=400),
    ]
    first = classify_interior_substructure_plans(plans)
    second = classify_interior_substructure_plans(plans)
    assert [plan.segment_type for plan in first] == [plan.segment_type for plan in second]
    assert [plan.complete for plan in first] == [plan.complete for plan in second]


def _oversized_residual_plan(row_base: int, *, with_attention: bool) -> StepPlan:
    """A non-complete residual whose layer count is LARGER than the reference
    body, so ``is_substructure_of_reference`` rejects it. This matches the
    350TPS DSV4 pattern where step splitting marked short ~1-layer plans as
    ``complete=True`` references, leaving a large multi-body residual that
    no reference can dominate.
    """

    if with_attention:
        signatures = _MLA_BODY_LAYER_SIGS + _MLA_BODY_LAYER_SIGS  # 6 layers, includes attention
    else:
        signatures = ("block_head|moe:moe.combinex1",) * 6
    return _step_plan(
        signatures,
        row_base=row_base,
        segment_type="step",
        complete=False,
    )


def test_oversized_residual_with_attention_absorbed_as_partial_body_window() -> None:
    """When an interior ``complete=False`` plan is sandwiched between two
    explained-type neighbors but its layer count exceeds every reference
    body (so it fails the strict-substructure check), it MUST still be
    absorbed as ``partial_body_window`` (if it carries primary attention)
    rather than left as the bare ``step`` segment type — otherwise
    ``validate_exact_cover`` raises ``interior_template_residual`` and
    aborts the whole segment stage.

    This is the exact mode that broke the 350TPS DSV4 sweep across many
    ranks (tp1/tp4/tp7 and beyond): the residual contained ~120 layers
    while the surrounding "complete" plans were short 1-layer
    substructures, so no reference dominated the residual.
    """

    plans = [
        _attention_body_plan(row_base=0),
        _oversized_residual_plan(row_base=100, with_attention=True),
        _attention_body_plan(row_base=200),
    ]
    classified = classify_interior_substructure_plans(plans)
    assert [plan.segment_type for plan in classified] == [
        "step",
        "partial_body_window",
        "step",
    ]
    assert classified[1].complete is False


def test_oversized_residual_without_attention_absorbed_as_runner_window() -> None:
    """The non-attention sibling of the previous test: an oversized
    residual sandwiched between explained-type neighbors but without
    primary attention must be absorbed as ``runner_window``.
    """

    plans = [
        _attention_body_plan(row_base=0),
        _oversized_residual_plan(row_base=100, with_attention=False),
        _attention_body_plan(row_base=200),
    ]
    classified = classify_interior_substructure_plans(plans)
    assert [plan.segment_type for plan in classified] == [
        "step",
        "runner_window",
        "step",
    ]
    assert classified[1].complete is False


def test_residual_sandwiched_by_partial_body_window_is_absorbed() -> None:
    """The sandwich check must accept any pair of explained neighbors —
    including already-classified ``partial_body_window`` / ``runner_window``
    plans — not just ``complete=True`` step plans. Otherwise, several
    adjacent residuals in a row would leave the inner ones unclassified.
    """

    explained_left = StepPlan(
        frames=_attention_substructure_plan(row_base=0).frames,
        main_frame_count=1,
        reason="left",
        segment_type="partial_body_window",
        complete=False,
    )
    explained_right = StepPlan(
        frames=_attention_substructure_plan(row_base=200).frames,
        main_frame_count=1,
        reason="right",
        segment_type="partial_body_window",
        complete=False,
    )
    plans = [
        _attention_body_plan(row_base=-100),
        explained_left,
        _oversized_residual_plan(row_base=100, with_attention=True),
        explained_right,
        _attention_body_plan(row_base=300),
    ]
    classified = classify_interior_substructure_plans(plans)
    # The middle plan must be absorbed even though its immediate
    # neighbors are ``partial_body_window`` (not ``complete``).
    assert classified[2].segment_type == "partial_body_window"
    assert classified[2].complete is False


def test_cached_helpers_are_consistent() -> None:
    """The pure-string helpers gain ``lru_cache`` decoration in Fix A.
    Repeated calls must return identical results, and Counter results
    from frame-level aggregators must be independent objects so callers
    can safely mutate them.
    """

    signature = _MLA_BODY_LAYER_SIGS[0]
    a = segm.layer_role_key(signature)
    b = segm.layer_role_key(signature)
    assert a == b
    assert segm.core_role_key(signature) == segm.core_role_key(signature)
    assert segm.layer_boundary_key(signature) == segm.layer_boundary_key(signature)
    assert segm.split_role_count("attention.mla.q_a_projx3") == ("attention.mla.q_a_proj", "3")
    assert segm.split_role_count("attention.mla.q_a_proj") == ("attention.mla.q_a_proj", "")

    counter1 = segm.coarse_core_components(signature)
    counter2 = segm.coarse_core_components(signature)
    assert counter1 == counter2
    # Callers (e.g. frame_coarse_core_components) mutate via Counter.update;
    # cached fresh Counter instances guarantee mutations cannot bleed
    # between calls.
    counter1["new_role"] = 99
    assert "new_role" not in segm.coarse_core_components(signature)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
