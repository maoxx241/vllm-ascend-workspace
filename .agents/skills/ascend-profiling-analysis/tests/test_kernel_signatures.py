"""Regression tests for ``knowledge/kernel_signatures.yaml``.

This file pins the contract between the runtime kernel matcher and the
YAML knowledge inventory. Since the matcher (``rules.categories_and_roles``)
is *driven by* the ``match_rules:`` section of this YAML, full Python↔YAML
parity is structural; what remains to guard:

1. **Structural** — the YAML parses, the production loader accepts it,
   every category / role the matcher can emit is a valid value in
   ``semantic_conventions.yaml``, and every category listed under
   ``kernels[].categories`` is a valid value too.
2. **Behavioural** — fed a curated set of profile kernel names
   (taken from real DSV2 / DSV4 / Qwen3 / Mamba traces — see source
   citations next to each case), ``categories_and_roles`` returns the
   categories the YAML claims it should. This is the *executable* form
   of the inventory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

YAML = pytest.importorskip("yaml", reason="pyyaml not installed; kernel sig test skipped")


_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _SKILL_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ascend_profile import common  # noqa: E402

KNOWLEDGE_DIR = _SCRIPTS / "ascend_profile" / "knowledge"
KERNEL_SIG_PATH = KNOWLEDGE_DIR / "kernel_signatures.yaml"
SEMCONV_PATH = KNOWLEDGE_DIR / "semantic_conventions.yaml"


def _load_yaml(path: Path) -> dict:
    return YAML.safe_load(path.read_text())


@pytest.fixture(scope="module")
def kernel_sig_doc() -> dict:
    return _load_yaml(KERNEL_SIG_PATH)


@pytest.fixture(scope="module")
def op_categories_enum() -> set[str]:
    doc = _load_yaml(SEMCONV_PATH)
    return set(doc["attributes"]["op_categories"]["values"])


def test_kernel_signatures_file_parses(kernel_sig_doc):
    assert kernel_sig_doc.get("version") == 1
    assert "kernels" in kernel_sig_doc
    assert isinstance(kernel_sig_doc["kernels"], list)
    # Every entry must at least have profile_name + categories.
    for entry in kernel_sig_doc["kernels"]:
        assert "profile_name" in entry, entry
        assert "categories" in entry, entry
        assert isinstance(entry["categories"], list), entry


def test_kernel_signatures_categories_in_enum(kernel_sig_doc, op_categories_enum):
    """Every category mentioned in kernel_signatures.yaml must be a valid enum value."""
    missing: set[str] = set()
    for entry in kernel_sig_doc["kernels"]:
        for cat in entry.get("categories", []):
            if cat not in op_categories_enum:
                missing.add(cat)
    assert not missing, (
        f"kernel_signatures.yaml references categories not declared in "
        f"semantic_conventions.yaml:op_categories: {sorted(missing)}"
    )


def test_deprecated_categories_table(kernel_sig_doc, op_categories_enum):
    """``deprecated_categories`` maps the old ``attention.csa*`` and
    ``attention.sfa*`` placeholders to the canonical paper-neutral
    kernel-level names (``attention.sparse_sharedkv``,
    ``attention.lightning_indexer``, ``attention.kv_compressor``, …).

    The RHS values must exist in the enum; the LHS values must NOT
    (they're deprecated)."""
    deprecated = kernel_sig_doc.get("deprecated_categories", {}) or {}
    for old, new in deprecated.items():
        assert old not in op_categories_enum, (
            f"deprecated category {old!r} still present in op_categories enum; "
            f"remove it from semantic_conventions.yaml"
        )
        assert new in op_categories_enum, (
            f"deprecated_categories: {old!r} maps to {new!r}, but {new!r} is "
            f"not in op_categories enum"
        )


# ----------------------------------------------------------------------------
# match_rules: the runtime matcher is driven by this YAML section, so full
# Python↔YAML parity is structural now. What still needs guarding:
#   * every category / role the matcher can emit is declared in
#     semantic_conventions.yaml (otherwise artifacts leak un-declared
#     values);
#   * the production loader's schema validation rejects malformed docs
#     with actionable errors.
# ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def match_rules_doc(kernel_sig_doc) -> dict:
    section = kernel_sig_doc.get("match_rules")
    assert isinstance(section, dict), (
        "kernel_signatures.yaml needs a match_rules: section — it is the "
        "runtime input of rules.categories_and_roles"
    )
    return section


def _match_rule_effects(doc: dict) -> tuple[set[str], set[str]]:
    categories: set[str] = set()
    roles: set[str] = set()
    for rule in doc["rules"]:
        for effect in rule.get("branches") or [rule]:
            categories.update(effect.get("add_categories") or ())
            roles.update(effect.get("add_roles") or ())
    return categories, roles


def test_match_rules_accepted_by_production_loader():
    """The shipped file must pass the same strict schema validation the
    runtime loader applies."""
    from ascend_profile import rules  # noqa: E402

    predicates, loaded = rules._load_match_rules(KERNEL_SIG_PATH)
    assert loaded, "match_rules.rules must be non-empty"
    ids = [rule["id"] for rule in loaded]
    assert len(ids) == len(set(ids)), "rule ids must be unique"
    assert "fused_mc2_kernel" in predicates


def test_match_rules_categories_in_enum(match_rules_doc, op_categories_enum):
    """Every category the matcher can emit (including branch effects) must
    be a declared op_categories enum value."""
    categories, _ = _match_rule_effects(match_rules_doc)
    missing = categories - op_categories_enum
    assert not missing, (
        f"match_rules can emit categories not declared in "
        f"semantic_conventions.yaml:op_categories: {sorted(missing)}"
    )


def test_match_rules_roles_in_enum(match_rules_doc):
    """Every role the matcher can emit must be a declared op_roles enum value."""
    roles_enum = set(_load_yaml(SEMCONV_PATH)["attributes"]["op_roles"]["values"])
    _, roles = _match_rule_effects(match_rules_doc)
    missing = roles - roles_enum
    assert not missing, (
        f"match_rules can emit roles not declared in "
        f"semantic_conventions.yaml:op_roles: {sorted(missing)}"
    )


def test_match_rules_schema_validation_errors(tmp_path):
    """Malformed match_rules docs must fail with clear RuntimeErrors."""
    from ascend_profile import rules  # noqa: E402

    def _doc(rule_extra=None, predicates=None):
        rule = {"id": "r1", "when": {"any_of": ["x"]}, "add_categories": ["compute.matmul"]}
        rule.update(rule_extra or {})
        return {"match_rules": {"predicates": predicates or {}, "rules": [rule]}}

    cases = [
        (_doc({"when": {"anyoff": ["x"]}}), "unknown condition keys"),
        (_doc({"when": {"predicate": "nope"}}), "undefined predicate"),
        (_doc({"id": "r1"}, None), None),  # valid baseline
        (
            {"match_rules": {"rules": [
                {"id": "r1", "when": {"any_of": ["x"]}, "add_categories": ["compute.matmul"]},
                {"id": "r1", "when": {"any_of": ["y"]}, "add_categories": ["compute.aux"]},
            ]}},
            "duplicate rule id",
        ),
        (
            {"match_rules": {"rules": [
                {"id": "r1", "when": {"any_of": ["x"]}, "branches": [
                    {"add_categories": ["compute.matmul"]},
                    {"when": {"any_of": ["y"]}, "add_categories": ["compute.aux"]},
                ]},
            ]}},
            "must be the last branch",
        ),
        (
            {"match_rules": {"rules": [{"id": "r1", "when": {"any_of": ["x"]}}]}},
            "no effect",
        ),
    ]
    for doc, needle in cases:
        path = tmp_path / "bad.yaml"
        path.write_text(YAML.safe_dump(doc), encoding="utf-8")
        if needle is None:
            rules._load_match_rules(path)
            continue
        with pytest.raises(RuntimeError, match=needle):
            rules._load_match_rules(path)


# ----------------------------------------------------------------------------
# Behavioural cases: real kernel names → expected category subset.
# ----------------------------------------------------------------------------
# Each case is `(kernel_name, must_have_categories, must_not_have_categories)`.
# ``task_type`` and ``accelerator_core`` are not used by the rule list for
# attention/MoE decisions, so they're left blank.

_CASES: list[tuple[str, set[str], set[str]]] = [
    # ---- Sparse-attention building blocks (shared by DSA + CSA at the
    #      kernel level; family resolution lives in attention_families.yaml).
    (
        "KVQuantSparseAttnSharedKV",
        {"attention.sparse_sharedkv"},
        {"attention.sparse_sharedkv.metadata", "attention.mla", "attention.flash_score"},
    ),
    (
        "KVQuantSparseAttnSharedKVMetadata",
        {"attention.sparse_sharedkv.metadata"},
        {"attention.sparse_sharedkv", "attention.mla"},
    ),
    (
        "QuantLightningIndexer",
        {"attention.lightning_indexer"},
        {"attention.mla", "attention.kv_compressor"},
    ),
    (
        "IndexerCompressEpilogV2",
        {"attention.lightning_indexer"},
        {"attention.mla"},
    ),
    (
        "Compressor",
        {"attention.kv_compressor"},
        {"attention.mla", "attention.lightning_indexer"},
    ),
    (
        "KVCompressEpilog",
        {"attention.kv_compressor"},
        set(),
    ),
    (
        "BatchMatmulTranspose",
        {"attention.sparse_attn.v_up_proj", "compute.matmul"},
        {"attention.mla.v_up_proj"},
    ),
    # ---- MLA (DSV2 / V3, also reused by DSA in V3.2 paper §4)
    (
        "MlaPreprocess",
        {"attention.mla", "attention.mla.preprocess"},
        {"attention.sparse_sharedkv", "attention.lightning_indexer"},
    ),
    (
        "MlaProlog",  # CANN canonical op name (per CANN op_list.md)
        {"attention.mla", "attention.mla.preprocess"},
        set(),
    ),
    (
        "MlaPrologV2WeightNz",
        {"attention.mla", "attention.mla.preprocess"},
        set(),
    ),
    (
        "KvRmsNormRopeCache",
        {"attention.mla.kv_norm_rope_cache", "attention.rope"},
        {"attention.sparse_sharedkv"},
    ),
    (
        "TransposeQuantBatchMatmul",
        {"attention.mla.v_up_proj", "compute.matmul"},
        {"attention.sparse_attn.v_up_proj"},
    ),
    # ---- KVComp overlay
    (
        "NpuHammingDistTopK",
        {"attention.kvcomp.topk", "attention.kvcomp"},
        {"attention.sparse_sharedkv", "attention.mla"},
    ),
    (
        "NpuSignBitsPack",
        {"attention.kvcomp.signpack"},
        set(),
    ),
    # NpuReshapeAndCacheBnsd is exercised down below (KVComp regression
    # guard with explicit ``attention.kv_cache_io`` exclusion).
    # ---- Dense flash-style score kernels (FIA / UnpadFA).
    # Per the CANN docs (aclnnFusedInferAttentionScore[V2-V5]) and
    # torch_npu.npu_fused_infer_attention_score, these support **MHA,
    # GQA, AND MLA** via num_key_value_heads — the kernel category
    # must therefore stay neutral (attention.flash_score) and must NOT
    # bake in an architecture-specific label like the previous
    # attention.gqa_or_mha. Architecture inference belongs in the
    # resolver, not in the kernel name.
    (
        "FusedInferAttentionScore",
        {"attention.flash_score"},
        {"attention.mla", "attention.sparse_sharedkv", "attention.gqa_or_mha"},
    ),
    (
        "FusedInferAttentionScoreV2",
        {"attention.flash_score"},
        {"attention.mla", "attention.sparse_sharedkv", "attention.gqa_or_mha"},
    ),
    (
        "FusedInferAttentionScoreV4",
        {"attention.flash_score"},
        {"attention.gqa_or_mha"},
    ),
    (
        "UnpadFlashAttention",
        {"attention.flash_score"},
        {"attention.gqa_or_mha"},
    ),
    # ATB-compiled bf16 variant of UnpadFlashAttention. Observed in
    # GLM4.5-0919 and Qwen2.5-VL-7B traces; same role as the canonical
    # name.
    (
        "UnpadFlashAttentionBF16NdKernel",
        {"attention.flash_score"},
        {"attention.gqa_or_mha", "attention.generic"},
    ),
    # ATB paged-attention score kernel (Qwen2.5-VL, GLM4.5-0919).
    # Functionally equivalent to FIA — feeds the dense path's paged
    # KV cache. Without this rule the resolver falls back to ``attn``.
    (
        "PagedAttentionMaskNdKernel",
        {"attention.flash_score"},
        {"attention.gqa_or_mha", "attention.generic", "attention.mla"},
    ),
    # ---- Linear / mamba / GDN
    (
        "CausalConv1d",
        {"attention.linear_or_mamba"},
        {"attention.flash_score", "attention.mla", "attention.sparse_sharedkv"},
    ),
    # Fix C: Qwen3-Next Gated DeltaNet causal 1D conv compiled by
    # vllm-ascend (per-kernel ``_npu_tiled_0`` suffix). Same role as
    # ``CausalConv1d`` above; pinned to make sure the longer token
    # form still folds onto the ``causalconv1d`` substring.
    (
        "_causal_conv1d_update_kernel_npu_tiled_0",
        {"attention.linear_or_mamba"},
        {"attention.flash_score", "attention.mla", "attention.sparse_sharedkv"},
    ),
    # Fix C: GDN gating kernel (Z gate computation). Triggers the
    # ``gdn`` substring rule.
    (
        "fused_gdn_gating_kernel_0",
        {"attention.linear_or_mamba"},
        {"attention.flash_score", "attention.mla", "attention.sparse_sharedkv"},
    ),
    # Fix C: GDN core recurrent rule kernel. Without an explicit
    # ``recurrentgateddelta`` substring rule this kernel used to fall
    # through entirely and never be tagged as attention, which broke
    # layer-block decomposition for Qwen3-Next-style hybrid models.
    (
        "RecurrentGatedDeltaRule_85669048de2f4ae7785fd8d81b12f70b_0",
        {"attention.linear_or_mamba"},
        {"attention.flash_score", "attention.mla", "attention.sparse_sharedkv"},
    ),
    # ---- RoPE companions
    (
        "InterleaveRope",
        {"attention.rope.interleave", "attention.rope"},
        set(),
    ),
    (
        "InPlacePartialRotaryMul",
        {"attention.rope.partial", "attention.rope"},
        {"attention.rope.interleave"},
    ),
    # MLA decode single-token rope (DSV3 trace).
    (
        "SingleRope",
        {"attention.rope.partial", "attention.rope"},
        {"attention.rope.interleave", "attention.rope.indexed"},
    ),
    # ATB / Triton generic RoPE variants — they all share the
    # ``attention.rope`` umbrella; no architecture-specific sub-kind
    # is inferred from name alone. Without these the layer-block
    # decomposer can't anchor on the rope kernels surrounding the
    # score op.
    (
        "RopeKernel",
        {"attention.rope"},
        {"attention.rope.interleave", "attention.rope.partial", "attention.rope.indexed"},
    ),
    (
        "AtbRopeKernel",
        {"attention.rope"},
        {"attention.rope.interleave", "attention.rope.partial", "attention.rope.indexed"},
    ),
    (
        "RopeWithSinCosCache_0_high_performance_20",
        {"attention.rope"},
        {"attention.rope.interleave", "attention.rope.partial"},
    ),
    (
        "RotaryPosEmbInfer_2453a9de_high_performance_22",
        {"attention.rope"},
        {"attention.rope.interleave", "attention.rope.partial"},
    ),
    (
        "rotary_pos_emb_22",
        {"attention.rope"},
        {"attention.rope.interleave", "attention.rope.partial"},
    ),
    (
        "_triton_rope",
        {"attention.rope"},
        {"attention.rope.interleave", "attention.rope.partial"},
    ),
    # ---- MLA preprocessing variants (Triton / Ascend-C custom).
    # Must reach attention.mla.kv_norm_rope_cache, exactly like
    # KvRmsNormRopeCache, so the family resolver returns ``mla``.
    (
        "split_qkv_rmsnorm_rope_kernel",
        {"attention.mla.kv_norm_rope_cache", "attention.rope"},
        {"attention.sparse_sharedkv", "attention.flash_score"},
    ),
    (
        "split_qkv_rmsnorm_rope_kernel_0",
        {"attention.mla.kv_norm_rope_cache", "attention.rope"},
        {"attention.sparse_sharedkv", "attention.flash_score"},
    ),
    # Fix C: ``fused_qkvzba_split_reshape_cat_kernel`` is the Qwen3-Next
    # Gated DeltaNet QKV + Z (gate) + B (beta) + A (alpha) projection
    # split. The ``zba`` suffix is GDN-specific and unrelated to the MLA
    # ``splitqkvrmsnormrope`` companion. Previous releases (incorrectly)
    # tagged it as ``attention.mla.kv_norm_rope_cache``, which made the
    # family resolver pick ``mla`` for prof_311 (a hybrid GDN+MoE trace
    # showing 1728 co-occurrences with causal_conv1d_update + fused_gdn
    # _gating). The corrected tag is ``attention.linear_or_mamba``.
    (
        "fused_qkvzba_split_reshape_cat_kernel_0",
        {"attention.linear_or_mamba"},
        {"attention.mla.kv_norm_rope_cache", "attention.mla", "attention.flash_score"},
    ),
    # ---- Paged-KV cache I/O (plain dense / ATB path, NOT KVComp).
    # Provides ``attention_aux`` role so layer-block decomposition
    # treats them as attention companions instead of leaving them as
    # untagged compute events.
    (
        "PagedCacheLoadNdKernel",
        {"attention.kv_cache_io"},
        {"attention.kvcomp.cache_write", "attention.mla.kv_norm_rope_cache"},
    ),
    (
        "ScatterPaKvCache",
        {"attention.kv_cache_io"},
        {"attention.kvcomp.cache_write"},
    ),
    (
        "ReshapeAndCacheNdKernel",
        {"attention.kv_cache_io"},
        {"attention.kvcomp.cache_write"},
    ),
    (
        # vllm-ascend Triton naming. fold_text strips underscores so
        # this folds to "reshapeandcache..." which is the substring we
        # match on.
        "reshape_and_cache_200000000",
        {"attention.kv_cache_io"},
        {"attention.kvcomp.cache_write"},
    ),
    # NpuReshapeAndCacheBnsd remains in the KVComp overlay (regression
    # guard so the new generic rule doesn't shadow the BNSD variant).
    (
        "NpuReshapeAndCacheBnsd",
        {"attention.kvcomp.cache_write"},
        {"attention.kv_cache_io"},
    ),
    # ---- MoE gating top-k (the genuine fused op only)
    (
        "MoeGatingTopKHash",
        {"moe.gating"},
        # MoeGatingTopKHash itself does NOT start with "hc", so the
        # block_head.mhc_prefix rule should not fire here.
        {"block_head.mhc_prefix"},
    ),
    # ---- HC* / MHC* — block_head structural prefix kernels.
    #      They appear in attention prologue AND moe routing prologue, so
    #      they must NOT be filed under moe.gating.
    (
        "HCPreSinkhorn",
        {"block_head.mhc_prefix"},
        {"moe.gating", "attention.mla"},
    ),
    (
        "HCPreInvRMS",
        {"block_head.mhc_prefix"},
        {"moe.gating"},
    ),
    (
        "HCPost",
        {"block_head.mhc_prefix"},
        {"moe.gating"},
    ),
    (
        "MhcRmsNorm",
        {"block_head.mhc_prefix", "normalization", "block_head"},
        {"moe.gating"},
    ),
    # ---- MoE dispatch / combine
    (
        "MoeDistributeDispatchV2",
        {"moe.dispatch"},
        {"moe.dispatch_expert_compute"},
    ),
    (
        "MoeDistributeCombineV2",
        {"moe.combine"},
        {"moe.dispatch_expert_compute"},
    ),
    (
        "DispatchFFNCombine",
        {"moe.dispatch_expert_compute"},
        {"moe.dispatch", "moe.combine"},
    ),
    (
        "DispatchGmmCombineDecode",
        {"moe.dispatch_expert_compute"},
        {"moe.dispatch", "moe.combine"},
    ),
    # ---- MoE expert matmul
    (
        "GroupedMatmul",
        {"moe.expert_matmul", "compute.matmul"},
        set(),
    ),
    # ---- Quant
    (
        "DynamicQuantV2",
        {"quant.dynamic", "compute.aux"},
        {"quant.mx"},
    ),
    (
        "DynamicMxQuant",
        {"quant.mx", "compute.aux"},
        {"quant.dynamic"},
    ),
    (
        "QuantBatchMatmulV3",
        {"compute.matmul", "quant.matmul"},
        set(),
    ),
    # ---- Communication
    (
        "hcom_allReduce",
        {"communication.collective", "communication.allreduce"},
        set(),
    ),
    (
        "hcom_allToAllV",
        {"communication.collective", "communication.alltoallv"},
        set(),
    ),
    # ---- Sampling
    (
        "ApplyTopKTopP",
        {"sampling.top_k_top_p", "sampling_or_selection"},
        set(),
    ),
]


@pytest.mark.parametrize("name,must_have,must_not_have", _CASES, ids=[c[0] for c in _CASES])
def test_kernel_classification_matches_knowledge(
    name: str, must_have: set[str], must_not_have: set[str]
) -> None:
    cats, _roles = common.categories_and_roles(name, "", "")
    cat_set = set(cats)
    missing = must_have - cat_set
    assert not missing, (
        f"kernel {name!r} expected to be tagged with {sorted(missing)}, "
        f"got {sorted(cat_set)}"
    )
    leaked = cat_set & must_not_have
    assert not leaked, (
        f"kernel {name!r} was tagged with categories that should NOT appear: "
        f"{sorted(leaked)} (full set: {sorted(cat_set)})"
    )


@pytest.mark.parametrize(
    "name,expected_categories,expected_roles",
    [
        # Regression: PR #50 review point 1. The broad "gmm" substring
        # rule used to fire on DispatchGmmCombineDecode and tag it as
        # standalone moe.expert_matmul, contradicting
        # kernel_signatures.yaml (categories: [moe.dispatch_expert_compute]
        # only). Lock down the *exact* category set for the two fused
        # MC2 single-kernel paths.
        (
            "DispatchGmmCombineDecode",
            {"moe.dispatch_expert_compute"},
            {"moe"},
        ),
        (
            "DispatchFFNCombine",
            {"moe.dispatch_expert_compute"},
            {"moe"},
        ),
    ],
)
def test_exact_categories_for_fused_mc2_kernels(name, expected_categories, expected_roles):
    """For kernels with a single declared category in
    ``kernel_signatures.yaml``, assert exact set equality. Subset-style
    assertions (used elsewhere in this file) would let an accidental
    extra category like ``moe.expert_matmul`` slip through unnoticed."""
    cats, roles = common.categories_and_roles(name, "", "")
    assert set(cats) == expected_categories, (
        f"kernel {name!r} expected exact category set {sorted(expected_categories)}, "
        f"got {sorted(cats)}"
    )
    assert set(roles) == expected_roles, (
        f"kernel {name!r} expected exact role set {sorted(expected_roles)}, "
        f"got {sorted(roles)}"
    )


def test_deprecated_category_names_not_emitted_by_python() -> None:
    """The earlier drafts coined three non-canonical name families:

    * ``attention.csa*``       — used as a generic catch-all.
    * ``attention.sfa*``       — used after a wrong subagent reading.
    * ``attention.gqa_or_mha`` — used as the kernel category for FIA /
       UnpadFlashAttention, but baked an architecture-specific name
       (GQA / MHA) into the kernel layer. CANN's FIA op explicitly
       supports MHA / GQA / MLA via ``num_key_value_heads`` so the
       kernel category is now the neutral ``attention.flash_score``.

    None of these may be emitted any more — the kernel rule list uses
    paper-neutral names; the paper-aligned architecture family
    (``csa`` / ``dsa`` / … / ``gqa_or_mha``) is resolved at the report
    layer from the *combination* of categories present in a block.
    """
    samples = [
        "KVQuantSparseAttnSharedKV",
        "KVQuantSparseAttnSharedKVMetadata",
        "Compressor",
        "KVCompressEpilog",
        "QuantLightningIndexer",
        "IndexerCompressEpilogV2",
        "BatchMatmulTranspose",
        # Kernels that previously emitted attention.gqa_or_mha — they
        # must now emit the neutral attention.flash_score instead.
        "FusedInferAttentionScore",
        "FusedInferAttentionScoreV2",
        "FusedInferAttentionScoreV4",
        "UnpadFlashAttention",
        "FlashAttention",
        "FlashAttentionScore",
    ]
    deprecated = {
        "attention.csa", "attention.csa.compressor", "attention.csa.indexer",
        "attention.csa.metadata",
        "attention.sfa", "attention.sfa.compressor", "attention.sfa.indexer",
        "attention.sfa.metadata", "attention.sfa.v_up_proj",
        "attention.gqa_or_mha",
    }
    for name in samples:
        cats, _ = common.categories_and_roles(name, "", "")
        leaked = set(cats) & deprecated
        assert not leaked, (
            f"kernel {name!r} still tagged with deprecated category "
            f"{sorted(leaked)} — common.py rule list out of sync with "
            f"kernel_signatures.yaml:deprecated_categories"
        )
