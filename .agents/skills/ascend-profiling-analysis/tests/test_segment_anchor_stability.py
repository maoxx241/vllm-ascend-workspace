"""Golden tests for layer-anchor stability diagnostics in the segment stage.

These tests pin the contracts added for anchor-boundary stability:

1. CSA/HCA ABAB-alternating layer streams cut a period-2 layer structure
   (exact ``minimal_exact_period`` evidence, no statistics).
2. Anchor degradation — the whole priority chain misses every attention
   marker and lands on normalization/block_head although the rank is expected
   to contain attention — is recorded explicitly (``anchor_degraded``,
   ``anchor_kind_used``, ``candidate_attention_kernels``, mode suffix,
   confidence downgrade) instead of passing silently.
3. Fused add+norm kernels (AddRmsNorm / AddRmsNormBias / GemmaRmsNorm class)
   never serve as the normalization fallback anchor; pure RmsNorm still does.
4. The decode layer-count invariant (one attention anchor group per
   transformer layer per complete step) records ok / mismatch / unknown, and
   a mismatch triggers a deterministic retry with the next anchor candidate.

Fixtures are synthetic event sequences; no real profiling artifacts are
required.  Style mirrors ``test_segment_validator.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _SKILL_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ascend_profile import segment as segm  # noqa: E402
from ascend_profile.segment import (  # noqa: E402
    anchor_kind,
    build_layers,
    build_segments_for_rank,
    build_uniform_step_plans,
    event_role,
    frames_from_selection,
    is_fused_norm_excluded,
    layer_anchor_candidates,
    layer_anchor_events,
    load_segmentation_rules,
    minimal_exact_period,
)
from ascend_profile.common import NormalizedEvent  # noqa: E402


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


def _build_test_layers(events: list[NormalizedEvent]) -> list[segm.LayerObservation]:
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


def _selection_event(row_idx: int) -> NormalizedEvent:
    return _event(
        row_idx,
        "ArgMax",
        categories=("sampling.argmax", "sampling_or_selection"),
        roles=("selection",),
    )


def _context(**overrides) -> dict:
    context = {
        "available": True,
        "model_name": "SyntheticModel",
        "source": "test",
        "expected_layers": None,
        "segment_hints": {},
        "features": [],
        "matched_reasons": ["test"],
    }
    context.update(overrides)
    return context


# ---------------------------------------------------------------------------
# segmentation_rules.yaml: fused-norm exclusion section
# ---------------------------------------------------------------------------


def test_segmentation_rules_expose_fused_norm_exclusions() -> None:
    """The fused-norm anchor exclusions come from the YAML knowledge base,
    folded to lowercase alnum tokens; pure ``rmsnorm`` must stay anchorable."""

    rules = load_segmentation_rules()
    tokens = rules["fused_norm_exclusions"]
    assert "addrmsnorm" in tokens
    assert "addrmsnormbias" in tokens
    assert "gemmarmsnorm" in tokens
    # Guard: the bare norm token must never be excluded — pure RmsNorm is the
    # legitimate normalization fallback anchor.
    assert "rmsnorm" not in tokens
    assert "norm" not in tokens
    assert all(token == token.lower() and token.isalnum() for token in tokens)


# ---------------------------------------------------------------------------
# Item 3: fused add+norm kernels are never normalization anchors
# ---------------------------------------------------------------------------


def test_fused_norm_excluded_from_normalization_anchor_kind() -> None:
    fused = _event(0, "aclnnAddRmsNorm", categories=("normalization",), roles=("normalization",))
    fused_bias = _event(1, "AddRmsNormBias", categories=("normalization",), roles=("normalization",))
    pure = _event(2, "aclnnRmsNorm", categories=("normalization",), roles=("normalization",))
    block_head_fused = _event(
        3,
        "aclnnAddRmsNorm",
        categories=("normalization", "block_head"),
        roles=("normalization", "block_head"),
    )

    assert is_fused_norm_excluded(fused)
    assert is_fused_norm_excluded(fused_bias)
    assert not is_fused_norm_excluded(pure)
    # Fused norm with only the normalization role loses anchor eligibility…
    assert anchor_kind(fused) is None
    assert anchor_kind(fused_bias) is None
    # …while pure RmsNorm remains the normalization fallback anchor…
    assert anchor_kind(pure) == "normalization"
    # …and a fused norm carrying block_head evidence is still a block_head
    # event (that branch is untouched).
    assert anchor_kind(block_head_fused) == "block_head"


def test_fused_norms_never_anchor_normalization_fallback() -> None:
    """In a norm-only rank, only pure RmsNorm kernels may anchor layers."""

    events = [
        _event(0, "aclnnAddRmsNorm", categories=("normalization",), roles=("normalization",)),
        _event(1, "aclnnRmsNorm", categories=("normalization",), roles=("normalization",)),
        _event(2, "aclnnAddRmsNormBias", categories=("normalization",), roles=("normalization",)),
        _event(3, "aclnnRmsNorm", categories=("normalization",), roles=("normalization",)),
    ]
    anchors = layer_anchor_events(events)
    assert [anchor.name_raw for anchor in anchors] == ["aclnnRmsNorm", "aclnnRmsNorm"]
    kinds = [kind for kind, _anchors in layer_anchor_candidates(events)]
    assert kinds == ["normalization"]


def test_all_fused_norm_rank_has_no_normalization_anchor() -> None:
    """A rank whose only norm evidence is fused add+norm yields no anchor at
    all instead of anchoring layer frequency on an irregular marker."""

    events = [
        _event(0, "aclnnAddRmsNorm", categories=("normalization",), roles=("normalization",)),
        _event(1, "aclnnAddRmsNorm", categories=("normalization",), roles=("normalization",)),
    ]
    assert layer_anchor_events(events) == ()
    _segments, _layers, _obs, _evd, hard_errors, strategy = build_segments_for_rank("rank0", events)
    assert strategy["mode"] == "no_structural_layers"
    assert hard_errors == []


# ---------------------------------------------------------------------------
# Item 4 golden: CSA/HCA ABAB alternation cuts a period-2 layer structure
# ---------------------------------------------------------------------------


def _csa_hca_events(step_count: int) -> list[NormalizedEvent]:
    """Each step: selection, one CSA layer (compressor+indexer+sparse), one
    HCA layer (compressor+flash).  10 rows per step."""

    events: list[NormalizedEvent] = []
    row = 0
    for _step in range(step_count):
        events.append(_selection_event(row))
        row += 1
        # CSA layer
        events.append(_event(row, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)))
        row += 1
        events.append(_event(row, "KvCompressor", categories=("attention.kv_compressor",), roles=("attention",)))
        row += 1
        events.append(_event(row, "LightningIndexer", categories=("attention.lightning_indexer",), roles=("attention",)))
        row += 1
        events.append(_event(row, "SparseSharedKvAttention", categories=("attention.sparse_sharedkv",), roles=("attention",)))
        row += 1
        events.append(_event(row, "aclnnMatmul", categories=("compute.matmul",), roles=("compute",)))
        row += 1
        # HCA layer
        events.append(_event(row, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)))
        row += 1
        events.append(_event(row, "KvCompressor", categories=("attention.kv_compressor",), roles=("attention",)))
        row += 1
        events.append(_event(row, "FusedInferAttentionScore", categories=("attention.flash_score",), roles=("attention",)))
        row += 1
        events.append(_event(row, "aclnnMatmul", categories=("compute.matmul",), roles=("compute",)))
        row += 1
    return events


def test_csa_hca_alternating_layers_yield_period_2() -> None:
    events = _csa_hca_events(step_count=4)
    layers = _build_test_layers(events)

    assert len(layers) == 8
    # Anchors alternate between the CSA sparse marker and the HCA flash score.
    assert [tuple(anchor.name_raw for anchor in layer.anchors) for layer in layers] == [
        ("SparseSharedKvAttention",),
        ("FusedInferAttentionScore",),
    ] * 4
    csa_key = layers[0].regime_key
    hca_key = layers[1].regime_key
    assert csa_key != hca_key
    assert "attention.lightning_indexerx1" in layers[0].signature
    assert "attention.sparse_sharedkvx1" in layers[0].signature
    assert "attention.flash_scorex1" not in layers[0].signature
    assert "attention.flash_scorex1" in layers[1].signature
    assert "attention.lightning_indexerx1" not in layers[1].signature

    # The layer stream is an exact period-2 repetition (ABAB).
    keys = tuple(layer.regime_key for layer in layers)
    assert minimal_exact_period(keys) == (csa_key, hca_key)

    # …and the uniform fast path cuts one (CSA, HCA) pair per step.
    selection_rows = segm.dedup_adjacent_event_rows(events, lambda event: event_role(event, "selection"))
    frames = frames_from_selection(layers, selection_rows)
    result = build_uniform_step_plans(frames)
    assert result is not None
    plans, strategy = result
    assert strategy["mode"] == "knowledge_uniform_period"
    assert strategy["layers_per_step"] == 2
    assert len(plans) == 4
    assert all(plan.complete and len(plan.main_layers) == 2 for plan in plans)
    for plan in plans:
        assert tuple(layer.regime_key for layer in plan.main_layers) == (csa_key, hca_key)


# ---------------------------------------------------------------------------
# Item 1: anchor degradation diagnostic
# ---------------------------------------------------------------------------


def _degraded_norm_rank_events() -> list[NormalizedEvent]:
    """Attention evidence exists only as metadata (never anchorable); the only
    structural markers are pure RmsNorm kernels."""

    return [
        _event(0, "SparseSharedKvMetadata", categories=("attention.sparse_sharedkv.metadata",), roles=("attention",)),
        _event(1, "aclnnRmsNorm", categories=("normalization",), roles=("normalization",)),
        _event(2, "aclnnRmsNorm", categories=("normalization",), roles=("normalization",)),
    ]


def test_anchor_degradation_marks_strategy_and_confidence(capsys) -> None:
    events = _degraded_norm_rank_events()
    context = _context(features=["mla"])  # model provably contains attention

    segments, _layers, _obs, _evd, hard_errors, strategy = build_segments_for_rank(
        "rank0", events, model_context=context
    )

    # No hard fail: artifacts are produced.
    assert hard_errors == []
    assert any(segment.segment_type == "step" for segment in segments)

    assert strategy["anchor_degraded"] is True
    assert strategy["anchor_kind_used"] == "normalization"
    assert strategy["mode"].endswith("_anchor_degraded")
    assert strategy["confidence"] == "low"

    candidates = strategy["candidate_attention_kernels"]
    assert [item["name"] for item in candidates] == ["aclnnRmsNorm"]
    assert candidates[0]["count"] == 2
    assert candidates[0]["op_categories"] == ["normalization"]
    assert candidates[0]["op_roles"] == ["normalization"]

    # Normalization anchor + no expected layer count => unvalidated.
    validation = strategy["layer_count_validation"]
    assert validation["status"] == "unknown"
    assert validation["anchor_kind"] == "normalization"
    assert validation["unvalidated"] is True

    # The degradation warning is visible on stderr progress output.
    err = capsys.readouterr().err
    assert "degraded" in err and "normalization" in err


def test_anchor_degradation_inferred_from_events_without_model_context(capsys) -> None:
    """Without model info, attention-role events excluded from anchoring still
    prove the rank should have contained attention markers."""

    events = _degraded_norm_rank_events()
    _segments, _layers, _obs, _evd, hard_errors, strategy = build_segments_for_rank(
        "rank0", events, model_context=None
    )
    assert hard_errors == []
    assert strategy["anchor_degraded"] is True
    assert strategy["anchor_kind_used"] == "normalization"
    assert "degraded" in capsys.readouterr().err


def test_no_degradation_flag_for_pure_norm_rank_without_attention_evidence() -> None:
    """A norm-only rank with no attention/moe/compute evidence anywhere is not
    expected to contain attention; the normalization anchor stays unflagged
    (confidence still low — normalization anchors are always low-trust)."""

    events = [
        _event(0, "aclnnRmsNorm", categories=("normalization",), roles=("normalization",)),
        _event(1, "aclnnRmsNorm", categories=("normalization",), roles=("normalization",)),
    ]
    _segments, _layers, _obs, _evd, _errors, strategy = build_segments_for_rank("rank0", events)
    assert "anchor_degraded" not in strategy
    assert not str(strategy["mode"]).endswith("_anchor_degraded")
    assert strategy["confidence"] == "low"
    assert strategy["layer_count_validation"]["unvalidated"] is True


# ---------------------------------------------------------------------------
# Item 2: layer-count invariant
# ---------------------------------------------------------------------------


def _flash_decode_events(step_count: int, layers_per_step: int) -> list[NormalizedEvent]:
    """Uniform decode steps: selection, then ``layers_per_step`` identical
    block_head + flash-attention + matmul layers."""

    events: list[NormalizedEvent] = []
    row = 0
    for _step in range(step_count):
        events.append(_selection_event(row))
        row += 1
        for _layer in range(layers_per_step):
            events.append(_event(row, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)))
            row += 1
            events.append(_event(row, "FusedInferAttentionScore", categories=("attention.flash_score",), roles=("attention",)))
            row += 1
            events.append(_event(row, "aclnnMatmul", categories=("compute.matmul",), roles=("compute",)))
            row += 1
    return events


def test_layer_count_invariant_ok_when_counts_match() -> None:
    events = _flash_decode_events(step_count=3, layers_per_step=4)
    context = _context(expected_layers=4, features=["dense_flash_attention"])

    segments, _layers, _obs, _evd, hard_errors, strategy = build_segments_for_rank(
        "rank0", events, model_context=context
    )

    assert hard_errors == []
    # expected_layers is available, so the model-guided path wins over the
    # uniform fast path; the invariant is validated on top of its plans.
    assert strategy["mode"] == "model_guided"
    validation = strategy["layer_count_validation"]
    assert validation["status"] == "ok"
    assert validation["expected"] == 4
    assert validation["accepted_targets"] == [4]
    assert validation["observed_per_step_median"] == 4
    assert validation["anchor_kind"].startswith("attention")
    assert "anchor_degraded" not in strategy
    assert "confidence" not in strategy
    steps = [segment for segment in segments if segment.segment_type == "step"]
    assert len(steps) == 3
    assert all(segment.main_layer_count == 4 for segment in steps)


def test_layer_count_invariant_mismatch_keeps_primary_and_records() -> None:
    """All anchor candidates fail the invariant -> keep the primary
    segmentation, record the mismatch, never raise."""

    events = _flash_decode_events(step_count=3, layers_per_step=4)
    context = _context(expected_layers=5, features=["dense_flash_attention"])

    segments, _layers, _obs, _evd, hard_errors, strategy = build_segments_for_rank(
        "rank0", events, model_context=context
    )

    assert hard_errors == []
    # Primary (attention) segmentation is kept: 3 steps of 4 layers.  The
    # model-guided path accepts the 4-layer bodies within its tolerance, and
    # the stricter invariant still records the mismatch on top.
    assert strategy["mode"] == "model_guided"
    steps = [segment for segment in segments if segment.segment_type == "step"]
    assert len(steps) == 3
    assert all(segment.main_layer_count == 4 for segment in steps)

    validation = strategy["layer_count_validation"]
    assert validation["status"] == "mismatch"
    assert validation["expected"] == 5
    assert validation["accepted_targets"] == [5]
    assert validation["observed_per_step_median"] == 4
    assert validation["mismatching_step_counts"] == [4]
    assert validation["anchor_kind"].startswith("attention")
    assert "layer_count_validation" in str(strategy.get("reason") or "")


def test_layer_count_invariant_mtp_nextn_target_accepted() -> None:
    """MTP/draft: expected + num_nextn_predict_layers is an accepted target."""

    events = _flash_decode_events(step_count=3, layers_per_step=5)
    context = _context(
        expected_layers=4,
        features=["dense_flash_attention"],
        segment_hints={"num_nextn_predict_layers": 1},
    )
    _segments, _layers, _obs, _evd, hard_errors, strategy = build_segments_for_rank(
        "rank0", events, model_context=context
    )
    assert hard_errors == []
    validation = strategy["layer_count_validation"]
    assert validation["status"] == "ok"
    assert validation["accepted_targets"] == [4, 5]


def _moe_rescue_events(step_count: int) -> list[NormalizedEvent]:
    """Attention anchors subdivide each step into 4 observations while two
    boundary-separated MoE gating kernels mark the 2 real transformer layers."""

    events: list[NormalizedEvent] = []
    row = 0
    for _step in range(step_count):
        events.append(_selection_event(row))
        row += 1
        for _layer in range(4):
            events.append(_event(row, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)))
            row += 1
            events.append(_event(row, "FusedInferAttentionScore", categories=("attention.flash_score",), roles=("attention",)))
            row += 1
            events.append(_event(row, "aclnnMatmul", categories=("compute.matmul",), roles=("compute",)))
            row += 1
        events.append(_event(row, "MoeGatingTopK", categories=("moe.gating",), roles=("moe",)))
        row += 1
        events.append(_event(row, "aclnnAddRmsNorm", categories=("block_head",), roles=("block_head",)))
        row += 1
        events.append(_event(row, "MoeGatingTopK", categories=("moe.gating",), roles=("moe",)))
        row += 1
    return events


def test_layer_count_invariant_fallback_anchor_rescues_mismatch() -> None:
    """When the primary attention anchor violates the invariant, the next
    evidence-priority candidate (MoE) is retried and adopted because it
    satisfies the expected per-step layer count."""

    events = _moe_rescue_events(step_count=3)
    context = _context(expected_layers=2, features=["dense_flash_attention", "moe"])

    segments, _layers, _obs, _evd, hard_errors, strategy = build_segments_for_rank(
        "rank0", events, model_context=context
    )

    assert hard_errors == []
    validation = strategy["layer_count_validation"]
    assert validation["status"] == "ok"
    assert validation["anchor_kind"] == "moe"
    assert validation["expected"] == 2
    assert validation["observed_per_step_median"] == 2
    assert "attention.non_companion" in str(validation.get("note") or "")

    steps = [segment for segment in segments if segment.segment_type == "step"]
    assert len(steps) == 3
    assert all(segment.main_layer_count == 2 for segment in steps)


# ---------------------------------------------------------------------------
# Manifest integration: mirrored rank-level fields
# ---------------------------------------------------------------------------


def test_segment_profile_manifest_mirrors_anchor_stability_fields(tmp_path: Path, monkeypatch) -> None:
    events = _degraded_norm_rank_events()
    context = _context(features=["mla"])
    monkeypatch.setattr(segm, "resolve_model_context", lambda **kwargs: context)

    manifest = segm.segment_profile(tmp_path, events=events, events_by_rank={"rank0": events})

    rank = manifest["rank_summaries"][0]
    assert rank["anchor_degraded"] is True
    assert rank["anchor_kind_used"] == "normalization"
    assert [item["name"] for item in rank["candidate_attention_kernels"]] == ["aclnnRmsNorm"]
    assert rank["confidence"] == "low"
    assert rank["layer_count_validation"]["status"] == "unknown"
    assert rank["segmentation_strategy"]["mode"].endswith("_anchor_degraded")

    # The manifest on disk carries the same fields.
    on_disk = json.loads((tmp_path / "segment_manifest.json").read_text(encoding="utf-8"))
    disk_rank = on_disk["rank_summaries"][0]
    assert disk_rank["anchor_degraded"] is True
    assert disk_rank["layer_count_validation"]["unvalidated"] is True
    assert (tmp_path / "step_segments.json").is_file()
    assert (tmp_path / "layer_segments.json").is_file()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
