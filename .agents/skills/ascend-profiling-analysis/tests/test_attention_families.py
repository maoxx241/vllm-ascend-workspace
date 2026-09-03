"""Family-resolution tests for attention.

These tests pin two contracts at once:

1. **The category-driven resolver** (``common.resolve_attention_family``)
   maps kernel bags to the right paper-aligned family. This is the
   executable form of the "must_have / must_not_have" signatures in
   ``knowledge/attention_families.yaml``.
2. **The HTML report uses the same resolver.**
   ``html_report.detect_attention_subtype`` is tested directly with
   fake ``Event``-shaped objects, so the test contract and the report
   output cannot drift apart.

Family names follow the DeepSeek papers (``mla`` / ``dsa`` / ``csa`` /
``hca`` / ``gqa_or_mha`` / …), NOT the CANN backend class name. DSA (V3.2)
and CSA (V4) both route through AscendSFABackend on Ascend, but they
are different paper architectures distinguished by whether a
Compressor kernel is present.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _SKILL_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ascend_profile import common, html_report  # noqa: E402


KNOWLEDGE_DIR = _SCRIPTS / "ascend_profile" / "knowledge"


def test_family_resolver_yaml_stays_inside_attention_family_enum():
    """``rules.resolve_attention_family`` is driven by
    ``attention_families.yaml:cheat_sheet.resolver``; every family label it
    can emit must be declared in semantic_conventions.yaml's
    ``attention_family`` enum (and the overlay suffix must be documented)."""
    yaml = pytest.importorskip("yaml", reason="pyyaml not installed")
    fam_doc = yaml.safe_load((KNOWLEDGE_DIR / "attention_families.yaml").read_text())
    sem_doc = yaml.safe_load((KNOWLEDGE_DIR / "semantic_conventions.yaml").read_text())
    enum = set(sem_doc["attributes"]["attention_family"]["values"])
    suffixes = set(sem_doc["attributes"]["attention_family"].get("suffix") or {})
    for step in fam_doc["cheat_sheet"]["resolver"]:
        assert step["family"] in enum, (
            f"resolver step emits family {step['family']!r} which is not in "
            f"semantic_conventions.yaml:attention_family"
        )
    overlay = fam_doc["cheat_sheet"]["overlay_rule"]
    assert overlay["suffix"] in suffixes, (
        f"overlay suffix {overlay['suffix']!r} not documented under "
        f"attention_family.suffix in semantic_conventions.yaml"
    )


def _categories_from_kernels(names: Iterable[str]) -> set[str]:
    cats: set[str] = set()
    for n in names:
        c, _ = common.categories_and_roles(n, "", "")
        cats.update(c)
    return cats


def _resolve_attention_family(names: Iterable[str]) -> str:
    """Convenience wrapper that runs the kernel names through the same
    pipeline the HTML report uses: ``categories_and_roles`` to get the
    category set, then ``common.resolve_attention_family``."""
    return common.resolve_attention_family(_categories_from_kernels(names))


# --- Minimal Event stand-in for the html_report.detect_attention_subtype test ---


@dataclass
class _FakeEvent:
    name: str
    rank_id: str = "rank0"
    row_idx: int = 0
    task_type: str = ""
    accel_core: str = ""
    # Used by the shape-refinement path; format mirrors the
    # ``Input Shapes`` cell from ``kernel_details.csv`` (``;``-separated
    # tensor shapes, comma-separated dims per tensor).
    raw_row: dict = None  # type: ignore[assignment]


@dataclass
class _FakeBlock:
    """Smallest object that ``detect_attention_subtype`` will accept.

    The function only reads ``b.events`` and slices it via
    ``events_in_row_range(b.events, row_start, row_end, rank_id)``.
    ``events_in_row_range`` filters by ``rank_id`` and ``row_idx``, so
    we set both consistently across the fake events.
    """

    events: list


def _events_for(names: Iterable[str]) -> list[_FakeEvent]:
    return [_FakeEvent(name=n, rank_id="rank0", row_idx=i) for i, n in enumerate(names)]


def _events_for_with_shapes(
    name_to_shapes: Iterable[tuple[str, str]],
) -> list[_FakeEvent]:
    """Build fake events carrying CANN-style ``Input Shapes`` cells, e.g.
    ``("FusedInferAttentionScore", "1,32,128;1,8,128;1,8,128")``.
    """
    out = []
    for idx, (name, shapes) in enumerate(name_to_shapes):
        out.append(
            _FakeEvent(
                name=name,
                rank_id="rank0",
                row_idx=idx,
                raw_row={"Input Shapes": shapes},
            )
        )
    return out


# Real-trace kernel bags. Each list is a *subset* of the kernels that
# appear in one attention block for the named family; the full block is
# bigger (RoPE, norm, BMM, etc.) but the listed ones are the unique
# signature kernels.

_FIXTURES: list[tuple[str, list[str], str]] = [
    # ---------- DeepSeek V2 / V3 MLA decode ----------
    (
        "DSV2_V3_MLA_decode",
        [
            "MlaPreprocess",
            "KvRmsNormRopeCache",
            "FusedInferAttentionScoreV2",
            "TransposeQuantBatchMatmul",
            "InterleaveRope",
        ],
        "mla",
    ),
    (
        "DSV2_V3_MLA_prefill",
        [
            "KvRmsNormRopeCache",
            "FusedInferAttentionScore",
            "InterleaveRope",
        ],
        "mla",
    ),
    (
        "DSV2_V3_MLA_with_canonical_CANN_name",
        # CANN op_list canonical names. We accept all three spellings.
        [
            "MlaProlog",
            "KvRmsNormRopeCache",
            "FusedInferAttentionScore",
        ],
        "mla",
    ),
    # ---------- DeepSeek V3.2 = DSA (NOT csa; NOT mla) ----------
    # DSA = Lightning Indexer + Sparse-SharedKV, NO Compressor.
    # DSA is built on MLA (V3.2 paper §4), so MLAPO and KvRmsNormRopeCache
    # still appear, but the sparse signatures must win.
    (
        "DSV3.2_DSA_decode",
        [
            "MlaPreprocess",
            "KvRmsNormRopeCache",
            "InterleaveRope",
            "KVQuantSparseAttnSharedKV",
            "QuantLightningIndexer",
            "BatchMatmulTranspose",
        ],
        "dsa",
    ),
    (
        "DSV3.2_DSA_prefill",
        [
            "KvRmsNormRopeCache",
            "QuantLightningIndexer",
            "KVQuantSparseAttnSharedKV",
            "IndexerCompressEpilogV2",
            "InPlacePartialRotaryMul",
        ],
        "dsa",
    ),
    # ---------- DeepSeek V4 = CSA (main layers) ----------
    # CSA = KV Compressor + Lightning Indexer + Sparse-SharedKV. The
    # presence of the Compressor kernel is what distinguishes V4 CSA
    # from V3.2 DSA.
    (
        "DSV4_CSA_prefill",
        [
            "KVQuantSparseAttnSharedKV",
            "KVQuantSparseAttnSharedKVMetadata",
            "QuantLightningIndexer",
            "QuantLightningIndexerMetadata",
            "Compressor",
            "KVCompressEpilog",
            "IndexerCompressEpilogV2",
            "InPlacePartialRotaryMul",
        ],
        "csa",
    ),
    (
        "DSV4_CSA_decode_with_MLAPO_reuse",
        [
            "MlaPreprocess",
            "KvRmsNormRopeCache",
            "InterleaveRope",
            "KVQuantSparseAttnSharedKV",
            "QuantLightningIndexer",
            "Compressor",
            "BatchMatmulTranspose",
        ],
        "csa",
    ),
    # ---------- DeepSeek V4 = HCA (alternating layers, heuristic) ----------
    # HCA = Compressor + dense FIA, no indexer, no sparse-sharedkv.
    (
        "DSV4_HCA_heuristic",
        [
            "Compressor",
            "KVCompressEpilog",
            "FusedInferAttentionScore",
            "InterleaveRope",
        ],
        "hca",
    ),
    # ---------- gqa_or_mha (dense flash-style attention) ------------
    #     Per the CANN docs FIA / UnpadFA support MHA AND GQA via
    #     ``num_key_value_heads`` — we honestly cannot pick between
    #     them from the kernel alone, hence ``gqa_or_mha``.
    (
        "Qwen3_dense_decode",
        [
            "FusedInferAttentionScore",
            "NpuRotaryEmbedding",
        ],
        "gqa_or_mha",
    ),
    (
        "Llama_dense_prefill",
        [
            "UnpadFlashAttention",
            "NpuRotaryEmbedding",
        ],
        "gqa_or_mha",
    ),
    (
        "Mistral_MHA_8b",  # MHA case (num_kv_heads == num_q_heads). Same kernels, same family.
        [
            "FusedInferAttentionScoreV2",
            "NpuRotaryEmbedding",
        ],
        "gqa_or_mha",
    ),
    # ---------- Linear / Mamba / GDN ----------
    (
        "Mamba2_attn_layer",
        ["CausalConv1d"],
        "linear",
    ),
    # ---------- KVComp overlays ----------
    (
        "DSV3.2_DSA_with_kvcomp",
        [
            "KVQuantSparseAttnSharedKV",
            "QuantLightningIndexer",
            "NpuHammingDistTopK",
            "NpuSignBitsPack",
        ],
        "dsa+kvc",
    ),
    (
        "DSV4_CSA_with_kvcomp",
        [
            "KVQuantSparseAttnSharedKV",
            "QuantLightningIndexer",
            "Compressor",
            "NpuHammingDistTopK",
        ],
        "csa+kvc",
    ),
    (
        "DSV2_MLA_with_kvcomp",
        [
            "KvRmsNormRopeCache",
            "FusedInferAttentionScoreV2",
            "NpuHammingDistTopK",
        ],
        "mla+kvc",
    ),
    (
        "Dense_with_kvcomp",
        [
            "FusedInferAttentionScore",
            "NpuHammingDistTopK",
        ],
        "gqa_or_mha+kvc",
    ),
]


@pytest.mark.parametrize(
    "label,kernels,expected_family",
    _FIXTURES,
    ids=[c[0] for c in _FIXTURES],
)
def test_attention_family_resolution(label, kernels, expected_family):
    """Drives the category-driven resolver (used by the HTML report)."""
    got = _resolve_attention_family(kernels)
    assert got == expected_family, (
        f"fixture {label}: kernels {kernels} resolved to family {got!r}, "
        f"expected {expected_family!r}"
    )


@pytest.mark.parametrize(
    "label,kernels,expected_family",
    _FIXTURES,
    ids=[c[0] for c in _FIXTURES],
)
def test_detect_attention_subtype_matches_resolver(label, kernels, expected_family):
    """The HTML report function must agree with the resolver on every
    fixture. Previously ``detect_attention_subtype`` ran its own raw
    kernel-name substring matcher, which diverged from the resolver on
    e.g. ``UnpadFlashAttention`` (resolver: ``gqa_or_mha``, old code: ``fa``)
    and on metadata-only sparse blocks. This test pins the contract."""
    block = _FakeBlock(events=_events_for(kernels))
    got = html_report.detect_attention_subtype(
        block,
        row_start=0,
        row_end=len(kernels),
        rank_id="rank0",
    )
    assert got == expected_family, (
        f"fixture {label}: detect_attention_subtype returned {got!r}, "
        f"expected {expected_family!r}"
    )


# ---------------------------------------------------------------------------
# Edge-case regressions surfaced by the PR #50 review (gpt-5.5)
# ---------------------------------------------------------------------------


def test_atb_paged_attention_mask_resolves_to_gqa_or_mha():
    """``PagedAttentionMaskNdKernel`` is the ATB compiled equivalent of
    FIA — same role, dense flash-style score. Before adding the
    ``pagedattentionmask`` substring to the flash_score rule in
    ``common.categories_and_roles`` it had no specific category and
    only fell through to ``attention.generic``, which the family
    resolver does not recognise → ``attn`` fallback. Real traces
    (Qwen2.5-VL, GLM4.5-0919) showed 100% of attention blocks
    falling back to ``attn``. Regression: profiling sweep on 131.
    """
    bag = ["PagedAttentionMaskNdKernel", "RopeWithSinCosCache_0_high_performance_20"]
    assert _resolve_attention_family(bag) == "gqa_or_mha"
    block = _FakeBlock(events=_events_for(bag))
    assert (
        html_report.detect_attention_subtype(block, 0, len(bag), "rank0")
        == "gqa_or_mha"
    )


def test_atb_unpadfa_bf16_still_resolves_to_gqa_or_mha():
    """``UnpadFlashAttentionBF16NdKernel`` is the ATB-compiled bf16
    variant of UnpadFlashAttention. ``fold_text`` reduces it to a
    string containing ``unpadflashattention``, so the existing dense
    flash-score rule should match. Pin this so future renames don't
    accidentally break the substring chain.
    """
    bag = ["UnpadFlashAttentionBF16NdKernel", "AtbRopeKernel"]
    assert _resolve_attention_family(bag) == "gqa_or_mha"


def test_split_qkv_rmsnorm_rope_kernel_resolves_to_mla():
    """DSV2-Lite (W8A8) and GLM4.7 emit a Triton custom kernel called
    ``split_qkv_rmsnorm_rope_kernel`` instead of the canonical
    ``KvRmsNormRopeCache``. The kernel serves the same role on the MLA
    pipeline (split + rmsnorm + rope + cache), so a block carrying it
    alongside FIA must resolve to ``mla``, NOT ``gqa_or_mha``.
    Regression: profiling sweep on 131 showed 100% of DSV2-Lite /
    GLM4.7 attention blocks falling back to ``gqa_or_mha`` because
    this kernel had no marker.
    """
    bag = ["split_qkv_rmsnorm_rope_kernel_0", "FusedInferAttentionScore"]
    assert _resolve_attention_family(bag) == "mla"


def test_fused_qkvzba_split_kernel_resolves_to_linear():
    """Fix C correction: ``fused_qkvzba_split_reshape_cat_kernel`` is the
    Qwen3-Next Gated DeltaNet QKV + Z (gate) + B (beta) + A (alpha)
    projection split. The ``zba`` markers are GDN's recurrence
    parameters, NOT MLA companions — the kernel must drive the
    ``linear`` family, not ``mla``.

    Earlier releases (pre-Fix C) tagged the kernel as
    ``attention.mla.kv_norm_rope_cache`` because its name *spelling*
    resembled the MLA ``splitqkvrmsnormrope`` companion; this caused
    Qwen3-Next hybrid traces (prof_311) to silently mis-resolve as
    pure MLA even though they actually contain GDN kernels
    (``RecurrentGatedDeltaRule_*``, ``fused_gdn_gating_kernel_0``,
    ``_causal_conv1d_update_kernel_*``).

    Pin the corrected behaviour so future kernel-rule changes can't
    accidentally re-link the kernel to MLA. Note the test also pairs
    the kernel with ``FusedInferAttentionScore`` — even when the GDN
    block runs next to a flash-score kernel the result must stay
    ``linear`` (``attention.linear_or_mamba`` takes precedence over
    bare ``attention.flash_score``).
    """
    bag = ["fused_qkvzba_split_reshape_cat_kernel_0", "FusedInferAttentionScore"]
    assert _resolve_attention_family(bag) == "linear"


def test_qwen3_next_gdn_companions_resolve_to_linear():
    """Fix C: Qwen3-Next Gated DeltaNet attention layer kernels — the
    full companion set observed in transfer/prof_311 — must resolve
    to the ``linear`` family. The block intentionally mixes the new
    ``recurrentgateddeltarule`` token, the ``gdn`` token, the
    ``causalconv1d`` token, AND the corrected ``qkvzbasplit`` token to
    pin all four detection paths in one shot.
    """
    bag = [
        "RecurrentGatedDeltaRule_85669048de2f4ae7785fd8d81b12f70b_0",
        "fused_gdn_gating_kernel_0",
        "_causal_conv1d_update_kernel_npu_tiled_0",
        "fused_qkvzba_split_reshape_cat_kernel_0",
    ]
    assert _resolve_attention_family(bag) == "linear"


def test_singlerope_does_not_break_mla_decode():
    """MLA decode emits ``SingleRope`` per layer per token. The kernel
    must add ``attention.rope.partial`` so the layer decomposer can
    anchor on it as an attention companion, but must NOT introduce
    any non-MLA family signal.
    """
    bag = ["KvRmsNormRopeCache", "SingleRope", "FusedInferAttentionScore"]
    assert _resolve_attention_family(bag) == "mla"


def test_pagedcacheio_does_not_alter_family_decision():
    """The new ``attention.kv_cache_io`` category is a companion role —
    must NEVER drive family decisions on its own. Adding paged
    cache I/O kernels to an MLA block keeps it ``mla``; to a dense
    block keeps it ``gqa_or_mha``.
    """
    mla_bag = ["KvRmsNormRopeCache", "FusedInferAttentionScore", "ScatterPaKvCache"]
    assert _resolve_attention_family(mla_bag) == "mla"
    dense_bag = ["PagedAttentionMaskNdKernel", "ReshapeAndCacheNdKernel"]
    assert _resolve_attention_family(dense_bag) == "gqa_or_mha"


def test_unpad_flash_attention_resolves_to_gqa_or_mha_not_fa():
    """``UnpadFlashAttention`` is the long-context branch of vllm-ascend's
    dense ``AscendAttentionBackend`` — NOT a separate FA backend. It
    must report as ``gqa_or_mha`` so the YAML / category contract
    holds. Regression: PR #50 review point 2 (gpt-5.5) and the user's
    follow-up that FIA / UnpadFA support both MHA and GQA via
    ``num_key_value_heads``, so a ``gqa``-only label was too narrow.
    """
    bag = ["UnpadFlashAttention", "NpuRotaryEmbedding"]

    assert _resolve_attention_family(bag) == "gqa_or_mha"
    block = _FakeBlock(events=_events_for(bag))
    assert (
        html_report.detect_attention_subtype(block, 0, len(bag), "rank0")
        == "gqa_or_mha"
    )


def test_fia_kernel_category_is_neutral_not_gqa_branded():
    """The CANN op ``FusedInferAttentionScore`` (FIA) supports MHA, GQA,
    AND MLA via ``num_key_value_heads`` — so the *kernel-level* category
    must be paper-neutral (``attention.flash_score``), not branded with
    one architecture. Architecture inference belongs in the resolver,
    not in the kernel category.
    """
    for name in (
        "FusedInferAttentionScore",
        "FusedInferAttentionScoreV2",
        "FusedInferAttentionScoreV4",
        "UnpadFlashAttention",
    ):
        cats, _ = common.categories_and_roles(name, "", "")
        cat_set = set(cats)
        assert "attention.flash_score" in cat_set, (
            f"{name!r}: expected attention.flash_score, got {sorted(cat_set)}"
        )
        assert "attention.gqa_or_mha" not in cat_set, (
            f"{name!r}: must not be tagged with the architecture-branded "
            f"attention.gqa_or_mha kernel category — the kernel supports "
            f"MHA / GQA / MLA via num_key_value_heads, the category must "
            f"stay neutral."
        )


def test_mla_decode_with_fia_still_resolves_to_mla():
    """The MLA decode path reuses FIA (with num_key_value_heads = 1)
    to compute the score. The bare ``attention.flash_score`` signature
    must therefore NOT pull an MLA block into ``gqa_or_mha`` — the
    MLA-specific companions (MlaProlog / KvRmsNormRopeCache /
    MLA V-up-proj) take precedence in the resolver decision order.
    """
    bag = [
        "MlaPreprocess",
        "KvRmsNormRopeCache",
        "FusedInferAttentionScoreV2",  # MLA decode invokes FIA
        "TransposeQuantBatchMatmul",
        "InterleaveRope",
    ]
    assert _resolve_attention_family(bag) == "mla"
    block = _FakeBlock(events=_events_for(bag))
    assert (
        html_report.detect_attention_subtype(block, 0, len(bag), "rank0") == "mla"
    )


def test_metadata_only_sparse_block_does_not_satisfy_sparse_signature():
    """A block that only contains the *metadata* sub-kernel must NOT
    classify as ``dsa`` / ``csa``. The main sparse-shared-KV category
    (``attention.sparse_sharedkv``) is required; the metadata category
    (``attention.sparse_sharedkv.metadata``) must not satisfy it.
    Regression: PR #50 review point 3.
    """
    bag = ["KVQuantSparseAttnSharedKVMetadata", "QuantLightningIndexer"]
    cats = _categories_from_kernels(bag)

    assert "attention.sparse_sharedkv" not in cats
    assert "attention.sparse_sharedkv.metadata" in cats
    assert common.resolve_attention_family(cats) != "dsa"
    assert common.resolve_attention_family(cats) != "csa"

    block = _FakeBlock(events=_events_for(bag))
    got = html_report.detect_attention_subtype(block, 0, len(bag), "rank0")
    assert got not in ("dsa", "csa"), (
        f"metadata-only sparse block classified as {got!r}; the main "
        "attention.sparse_sharedkv category must be required."
    )


def test_compressor_plus_dense_fia_alone_resolves_to_hca():
    """V4 HCA-heuristic: Compressor + dense FIA, no indexer, no
    sparse-shared-KV. Verifies the resolver agrees with the cheat-sheet
    step 2.
    """
    bag = ["Compressor", "KVCompressEpilog", "FusedInferAttentionScore"]
    assert _resolve_attention_family(bag) == "hca"
    block = _FakeBlock(events=_events_for(bag))
    assert html_report.detect_attention_subtype(block, 0, len(bag), "rank0") == "hca"


def test_compressor_indexer_sparse_resolves_to_csa():
    """V4 CSA main layer: all three sparse-attention building blocks
    plus a Compressor. Verifies the resolver agrees with the cheat-sheet
    step 1.
    """
    bag = ["Compressor", "QuantLightningIndexer", "KVQuantSparseAttnSharedKV"]
    assert _resolve_attention_family(bag) == "csa"
    block = _FakeBlock(events=_events_for(bag))
    assert html_report.detect_attention_subtype(block, 0, len(bag), "rank0") == "csa"


def test_indexer_plus_sparse_no_compressor_resolves_to_dsa():
    """V3.2 DSA: Lightning Indexer + Sparse-SharedKV, no Compressor.
    Verifies the resolver agrees with the cheat-sheet step 3.
    """
    bag = ["QuantLightningIndexer", "KVQuantSparseAttnSharedKV"]
    assert _resolve_attention_family(bag) == "dsa"
    block = _FakeBlock(events=_events_for(bag))
    assert html_report.detect_attention_subtype(block, 0, len(bag), "rank0") == "dsa"


def test_csa_vs_dsa_distinguished_by_compressor():
    """The Compressor kernel is the *only* difference between a V3.2 DSA
    layer and a V4 CSA layer at the kernel level. Drop the Compressor
    from a CSA bag → it must reclassify as DSA. Add a Compressor back →
    must reclassify as CSA.
    """
    csa_bag = ["KVQuantSparseAttnSharedKV", "QuantLightningIndexer", "Compressor"]
    dsa_bag = ["KVQuantSparseAttnSharedKV", "QuantLightningIndexer"]

    assert _resolve_attention_family(csa_bag) == "csa"
    assert _resolve_attention_family(dsa_bag) == "dsa"


def test_mla_signature_disjoint_from_sparse():
    """A pure MLA bag (no Compressor, no Indexer, no Sparse-SharedKV)
    must resolve to ``mla``. A pure sparse bag must NOT pick up the
    ``mla`` family label even when it shares the MLA preprocess kernel.
    """
    mla_only = ["MlaPreprocess", "KvRmsNormRopeCache", "TransposeQuantBatchMatmul"]
    dsa_with_mla_reuse = [
        "MlaPreprocess",
        "KvRmsNormRopeCache",
        "KVQuantSparseAttnSharedKV",
        "QuantLightningIndexer",
    ]
    csa_with_mla_reuse = dsa_with_mla_reuse + ["Compressor"]

    assert _resolve_attention_family(mla_only) == "mla"
    assert _resolve_attention_family(dsa_with_mla_reuse) == "dsa"
    assert _resolve_attention_family(csa_with_mla_reuse) == "csa"


def test_block_head_hc_prefix_does_not_pollute_attention_family():
    """The HC* block-head prefix kernels appear before BOTH attention
    and MoE blocks. Adding them to a DSA bag must not flip the family,
    must not introduce moe.gating, must not pretend to be SFA-specific.
    """
    dsa_with_hc = [
        "HCPreSinkhorn",
        "HCPreInvRMS",
        "HCPost",
        "KVQuantSparseAttnSharedKV",
        "QuantLightningIndexer",
    ]
    assert _resolve_attention_family(dsa_with_hc) == "dsa"
    cats = _categories_from_kernels(dsa_with_hc)
    assert "block_head.mhc_prefix" in cats
    assert "moe.gating" not in cats


# ---------------------------------------------------------------------------
# Shape-based refinement of the gqa_or_mha umbrella label.
#
# CANN's FIA op (aclnnFusedInferAttentionScore[V2-V5]) supports MHA, GQA,
# and MLA via num_key_value_heads. The category resolver can only return
# the umbrella ``gqa_or_mha`` from kernel signatures alone — but
# ``kernel_details.csv:Input Shapes`` records the Q/K tensors per event,
# so we run a best-effort refinement to upgrade the umbrella label to
# ``mha`` / ``gqa`` / ``mqa`` when shapes pass sanity checks. These tests
# pin the contract for that refinement.
# ---------------------------------------------------------------------------


def test_shape_refine_mha_when_q_kv_heads_match():
    """num_q_heads == num_kv_heads → MHA. Example: GPT-2 small (q=12, kv=12)."""
    events = _events_for_with_shapes([
        # CANN ABI: (query, key, value, ...). Shapes are
        # ``;``-separated tensors, dims comma-separated within.
        ("FusedInferAttentionScore", "1,12,64;1,512,12,64;1,512,12,64"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "mha"


def test_shape_refine_gqa_when_q_heads_multiple_of_kv():
    """num_q > num_kv with integer ratio → GQA. Example: Llama-3 8B
    (q=32, kv=8)."""
    events = _events_for_with_shapes([
        ("FusedInferAttentionScoreV2", "1,32,128;1,1024,8,128;1,1024,8,128"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "gqa"


def test_shape_refine_mqa_when_kv_heads_eq_one():
    """num_kv_heads == 1 with num_q > 1 → MQA. Example: PaLM-style MQA
    (q=16, kv=1)."""
    events = _events_for_with_shapes([
        ("FusedInferAttentionScore", "1,16,128;1,512,1,128;1,512,1,128"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "mqa"


def test_shape_refine_falls_back_when_input_shapes_missing():
    """Acl-graph compilation can wipe Input Shapes from some rows. With
    no shape evidence the refinement must return the umbrella label,
    NOT pretend to know.
    """
    events = _events_for(["FusedInferAttentionScore", "NpuRotaryEmbedding"])
    assert common.refine_dense_attention_from_shapes(events) == "gqa_or_mha"


def test_shape_refine_falls_back_on_invalid_head_dim():
    """Sanity check: if the trailing axis (head_dim) isn't a plausible
    value, drop the candidate (we probably latched onto a mask / pse /
    scale tensor) and return the umbrella label.
    """
    events = _events_for_with_shapes([
        # head_dim = 7 is not in the valid set → refinement must give up.
        ("FusedInferAttentionScore", "1,32,7;1,1024,8,7"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "gqa_or_mha"


def test_shape_refine_falls_back_on_mismatched_qk_head_dim():
    """Q.head_dim and K.head_dim must agree on FIA / UnpadFA; if they
    don't we picked up the wrong tensors and bail out.
    """
    events = _events_for_with_shapes([
        ("FusedInferAttentionScore", "1,32,128;1,1024,8,64"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "gqa_or_mha"


def test_shape_refine_majority_vote_across_events():
    """When a block contains multiple FIA events (e.g. prefill + decode
    sub-blocks), the refinement votes across them. Two GQA-shape events
    + one shape-missing event → GQA.
    """
    events = _events_for_with_shapes([
        ("FusedInferAttentionScore",   "1,32,128;1,1024,8,128"),
        ("FusedInferAttentionScoreV2", "1,32,128;1,1024,8,128"),
    ])
    events.append(_FakeEvent(name="FusedInferAttentionScore", row_idx=99))  # no shape
    assert common.refine_dense_attention_from_shapes(events) == "gqa"


def test_shape_refine_ignores_non_flash_score_events():
    """Only FIA / UnpadFA events feed the vote — other kernels in the
    block are ignored, even if they happen to carry input shapes.
    """
    events = _events_for_with_shapes([
        ("NpuRotaryEmbedding",   "1,32,128;1,32,128"),  # ignored
        ("NormalizationKernel",  "1,4096"),             # ignored
    ])
    assert common.refine_dense_attention_from_shapes(events) == "gqa_or_mha"


# ---------------------------------------------------------------------------
# Fix F regressions: paged-K layouts (prefill direction) + ATB paged-attention
# ---------------------------------------------------------------------------


def test_shape_refine_bails_when_kv_heads_exceed_q_heads():
    """Real MHA / GQA / MQA always satisfies ``num_kv_heads <= num_q_heads``.
    A candidate where the parsed ``num_kv_heads`` exceeds ``num_q_heads``
    is therefore guaranteed to be a paged-K layout — the second-to-last
    axis of the K tensor encodes the cache block_size, not the real
    number of KV heads.

    Concrete trace this guards: nextprof FIA prefill shapes
    ``32768,8,256 ; 1950,128,256``. The decode-direction paged guard
    (``K[0] >= Q[0]*GUARD_RATIO``) does NOT fire here because
    ``1950 < 32768``, so without this invariant check the refinement
    would emit ``(q=8, kv=128)``, fail every mha/gqa/mqa rule, silently
    skip, and "happen to" return ``gqa_or_mha`` for the wrong reason.
    Pin the explicit bail-out so future refinement-rule changes can't
    accidentally turn the silent skip into a wrong-answer label.
    """
    events = _events_for_with_shapes([
        ("FusedInferAttentionScore", "32768,8,256;1950,128,256;1950,128,256"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "gqa_or_mha"


def test_shape_refine_recognises_pagedattentionmask_kernel():
    """``PagedAttentionMaskNdKernel`` is the ATB compiled equivalent of
    FIA / UnpadFA on the dense flash-score path (qwen25vl, glm45_0919).
    The shape-refinement pass used to ignore it because the
    ``_FLASH_SCORE_NAME_TOKENS`` tuple only listed the FIA / UnpadFA /
    FlashAttention* names — so a Qwen2.5-VL dense block that happened
    to carry a non-paged FIA shape would still get the umbrella
    ``gqa_or_mha`` for the wrong reason. The kernel name is now
    explicitly recognised; non-paged shapes refine, paged-K shapes
    fall back gracefully.
    """
    # Non-paged MHA shapes: should refine to ``mha``.
    events = _events_for_with_shapes([
        ("PagedAttentionMaskNdKernel", "1,12,64;1,512,12,64;1,512,12,64"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "mha"
    # Paged-K layout: must still fall back (no kv_heads visible).
    events = _events_for_with_shapes([
        ("PagedAttentionMaskNdKernel", "32768,8,256;1950,128,256"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "gqa_or_mha"


def test_shape_refine_handles_unpad_flash_attention_bf16_nd_kernel():
    """``UnpadFlashAttentionBF16NdKernel`` (ATB bf16 variant) is the
    flash-score kernel used by Qwen2.5-VL-7B. Real trace shapes
    ``4888,16,128 ; 4888,16,128`` give q=kv=16 → MHA. Pin this so the
    matching token in ``_FLASH_SCORE_NAME_TOKENS`` keeps catching the
    kernel after future refactors.
    """
    events = _events_for_with_shapes([
        ("UnpadFlashAttentionBF16NdKernel", "4888,16,128;4888,16,128;4888,16,128"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "mha"


def test_events_in_row_range_is_inclusive_on_both_ends():
    """Regression for the bug that hid every attention block's FIA
    event from the resolver.

    ``block_segments.json`` (and ``layer_segments.json`` /
    ``step_segments.json``) record ``row_start`` / ``row_end`` as
    **inclusive** boundaries — e.g. a block ``(row_start=106,
    row_end=121, event_count=16)`` covers exactly 16 rows. The
    original ``events_in_row_range`` implementation treated it as
    half-open ``[row_start, row_end)``, silently dropping the row
    sitting on ``row_end``.

    In vLLM-Ascend the closing FIA / UnpadFlashAttention score kernel
    of an attention block is exactly that last row, so the half-open
    query made ``detect_attention_subtype`` see no
    ``attention.flash_score`` categories and return the ``attn``
    fallback instead of ``mha`` / ``gqa_or_mha`` / etc.

    Pin both the inclusive contract AND the resulting attention
    sub-type resolution to a real Qwen3.5 prefill shape (FIA on the
    last row, ``num_q == num_kv == 4``).
    """
    # Layout mirrors a vLLM-Ascend prefill attention block: prep kernels
    # first, then the closing FIA score on the final row.
    events = [
        _FakeEvent(name="NpuRotaryEmbedding", rank_id="rank0", row_idx=106),
        _FakeEvent(name="ReshapeAndCache",    rank_id="rank0", row_idx=107),
        _FakeEvent(
            name="aclnnFlashAttentionVarLenScore_FlashAttentionScore_FlashAttentionScore",
            rank_id="rank0",
            row_idx=121,  # last row of the block — must be reachable.
            raw_row={"Input Shapes": "1620,4,128;1620,4,128;1620,4,128"},
        ),
    ]
    block = _FakeBlock(events=events)

    # 1. The slice itself must include row_end=121.
    sliced = html_report.events_in_row_range(events, 106, 121, "rank0")
    assert len(sliced) == 3, (
        "events_in_row_range must include row_end; this is the bug that "
        "made every attention block lose its closing FIA event"
    )
    assert sliced[-1].row_idx == 121

    # 2. End-to-end: the closing FIA on the last row must drive the
    # category resolver, and shape refinement must lift the umbrella
    # ``gqa_or_mha`` to ``mha`` (Q[0]==K[0]==1620, num_q==num_kv==4).
    assert (
        html_report.detect_attention_subtype(block, 106, 121, "rank0") == "mha"
    )


def test_detect_attention_subtype_refines_gqa_or_mha_to_mha():
    """End-to-end through ``html_report.detect_attention_subtype``: a
    dense FIA block with MHA shapes should report ``mha``, NOT
    ``gqa_or_mha``.
    """
    events = _events_for_with_shapes([
        ("FusedInferAttentionScore", "1,12,64;1,512,12,64;1,512,12,64"),
        ("NpuRotaryEmbedding",       ""),
    ])
    block = _FakeBlock(events=events)
    assert (
        html_report.detect_attention_subtype(block, 0, len(events), "rank0") == "mha"
    )


def test_detect_attention_subtype_keeps_gqa_or_mha_when_shapes_missing():
    """End-to-end: dense FIA block with no shapes → report stays at the
    umbrella ``gqa_or_mha``.
    """
    events = _events_for(["FusedInferAttentionScore", "NpuRotaryEmbedding"])
    block = _FakeBlock(events=events)
    assert (
        html_report.detect_attention_subtype(block, 0, len(events), "rank0")
        == "gqa_or_mha"
    )


def test_shape_refinement_does_not_override_mla_decision():
    """An MLA decode block reuses FIA with ``num_kv_heads = 1`` (MQA-
    style shape). The category resolver already picked ``mla``; shape
    refinement must NOT downgrade it to ``mqa`` because the decision
    order puts category-based MLA detection first.
    """
    # category-side: MLA companions present → resolver returns "mla"
    events = _events_for_with_shapes([
        ("MlaPreprocess",                ""),
        ("KvRmsNormRopeCache",           ""),
        ("FusedInferAttentionScoreV2",   "1,16,128;1,512,1,128"),  # would refine to mqa
    ])
    block = _FakeBlock(events=events)
    assert (
        html_report.detect_attention_subtype(block, 0, len(events), "rank0") == "mla"
    )


def test_shape_refine_rejects_paged_kv_cache_layout():
    """Real Qwen3.5 35b decode block: Q is batch-major
    ``[B, num_q_heads, head_dim]``, but K is the *paged* KV cache
    ``[num_blocks, block_size, head_dim]`` where the second-to-last
    axis is ``block_size``, not ``num_kv_heads``. The refinement must
    refuse this layout (we cannot recover num_kv_heads from a paged
    K), and the report must keep the ``gqa_or_mha`` umbrella label.

    Real shapes pulled from
    ``/tmp/prof_qwen35_hardcase_20260417_a`` kernel_details.csv:
        Q = [4, 4, 256]       (4 decode tokens, 4 q heads/rank, head_dim=256)
        K = [10000, 128, 256] (10000 blocks, block_size=128, head_dim=256)
    """
    events = _events_for_with_shapes([
        ("FusedInferAttentionScoreV2", "4,4,256;10000,128,256;10000,128,256"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "gqa_or_mha"


def test_shape_refine_accepts_non_paged_3d_with_matching_batch_axes():
    """Real Qwen3.5 35b prefill block: Q/K/V all share the standard
    batch-major 3D layout ``[total_tokens, num_heads, head_dim]``.
    Refinement must NOT mistake this for paged; with Q[0]==K[0] and
    num_q==num_kv, the answer is ``mha``.

    Real shape pulled from kernel_details.csv:
        Q = K = V = [1620, 4, 128]   (1620 tokens, 4 heads/rank, head_dim=128)
    """
    events = _events_for_with_shapes([
        ("FlashAttentionScore", "1620,4,128;1620,4,128;1620,4,128"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "mha"


def test_shape_refine_rejects_5d_unknown_layout():
    """5D+ tensors don't match any layout we know how to read; bail
    out rather than guess.
    """
    events = _events_for_with_shapes([
        ("FusedInferAttentionScore", "2,4,8,16,128;2,4,8,16,128"),
    ])
    assert common.refine_dense_attention_from_shapes(events) == "gqa_or_mha"


def test_shape_refinement_preserves_kvc_suffix():
    """The ``+kvc`` suffix from KVComp overlay must survive shape
    refinement. ``gqa_or_mha+kvc`` with GQA shapes → ``gqa+kvc``.
    """
    events = _events_for_with_shapes([
        ("FusedInferAttentionScore", "1,32,128;1,1024,8,128"),
        ("NpuHammingDistTopK",       ""),
    ])
    block = _FakeBlock(events=events)
    assert (
        html_report.detect_attention_subtype(block, 0, len(events), "rank0")
        == "gqa+kvc"
    )
