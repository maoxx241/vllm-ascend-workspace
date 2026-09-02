# Block Taxonomy

This document defines how the analyzer decomposes every layer into a
small fixed set of **blocks** so that the report can talk about
"attention vs FFN/MoE" without inventing a sub-layer hierarchy that the
profile cannot prove.

## 1. Block kinds

A block is a contiguous range of `kernel_details.csv` rows inside one
`LayerSegment`.  The taxonomy is intentionally coarse:

| Kind | Anchor | Typical content |
|---|---|---|
| `attention` | event with role `attention` or `attention_aux` | QKV projection matmul, FlashAttention / MLA / CSA / linear-attention kernel, O projection matmul, attention-side norms |
| `ffn` | event with role `compute` only (no attention/moe events present in same layer, or after the attention anchor) | dense FFN matmul (gate / up / down) + activation (SwiGLU etc.) |
| `moe` | event with role `moe`, **or** `op_type == "mix_comm_aiv"` | gating, dispatch, expert matmul (`GroupedMatmul` / `GMM`), combine; includes the AIV-side work of fused `DispatchFFNCombine` / `MoeDistributeDispatch` / `MoeDistributeCombine` kernels |
| `aicpu` | layer with no AI-Core / AI-Vector kernel and majority `aicpu` events | sampling, host-bound bookkeeping; flagged separately so it never inflates "attention" or "ffn" stats |
| `other` | residual case (no anchors at all, mostly normalization / block_head) | rare; usually a sub-anchor partial layer |

A standard transformer layer therefore reduces to **at most two blocks**:

* dense layer    -> `attention` + `ffn`
* MoE layer      -> `attention` + `moe`
* companion layer -> `moe` only (no attention) or `ffn` only

## 2. Companion layer

A layer is a *companion* layer iff it has no `attention` block.  In
practice this happens for:

* eager-mode bookkeeping passes that run alongside a graph-mode forward;
* speculative-decoding head layers that contain only an MoE / FFN body;
* AICPU-only layers (sampling / argmax windows that the segmenter
  picked up);
* DeepSeek-V4 first dense layers in some ranks (these may also surface
  as `attention -> ffn` so check both columns before drawing
  conclusions).

The flag is exposed in `block_summary.csv:companion_layer`,
`layer_summary.csv:companion_layer`, and as the `Companion` column in
the report's *Layer And Block View* section.  The intent is "do not
silently mix dummy / aux runs into the main numbers"; consumers may
still want to compare companion vs main classes side-by-side.

## 3. Boundary rule

The split between `attention` and `ffn`/`moe` is **anchor-based**, never
name-based:

1. Locate the row range of attention events (first → last) and MoE
   events (first → last) inside the layer.
2. *No attention*: the entire layer is one block whose kind matches the
   non-attention anchor (`moe` if any MoE events present, otherwise
   `ffn`, falling back to `aicpu` / `other`).
3. *No MoE, has attention*: split at the **last attention row**.
   Everything ≤ last_attn becomes `attention` (this absorbs the QKV
   projection at the start and the O projection at the end); everything
   after becomes `ffn`.
4. *Both present*: split at the **midpoint between last attention row
   and first MoE row**.  This places the post-attention norm / O-proj
   in the attention block and the gating / dispatch / expert matmul /
   combine on the MoE side.
5. *Interleaved (rare, usually a misclassified op)*: split at the first
   MoE row minus 1.

Why row-midpoint instead of time-midpoint: row order matches the
on-device sequencing in `kernel_details.csv` and is independent of
stream skew, so two ranks executing the same layer always agree on the
boundary.

## 3a. Attention sub-family (annotation, not a separate `block_kind`)

The block kind stays at the coarse `attention` granularity, but the
analyser annotates each attention block with the **attention family**
that produced it.  Detection is signature-based and uses category labels
emitted in `op_categories`, never single-kernel substring matches.  The
full rules live in `knowledge/attention_families.yaml`; the decision
order is:

Family names follow the **DeepSeek papers**, not the CANN backend
class (DSA and CSA both route through `AscendSFABackend` on Ascend,
but they are different paper architectures distinguished by whether
the Compressor kernel is present). The decision is implemented exactly
once in `rules.resolve_attention_family(categories)`, where
`categories` is the union of `op_categories` emitted by
`categories_and_roles` for the events in the block. The HTML report
function `detect_attention_subtype` is a thin wrapper around that
resolver, so the report and the unit-test contract cannot drift apart.

1. `attention.kv_compressor` AND `attention.lightning_indexer` AND
   `attention.sparse_sharedkv` → **CSA** (DeepSeek-V4 main layers,
   "Compressed Sparse Attention" per the V4 paper).
2. else `attention.kv_compressor` AND `attention.flash_score` with NO
   indexer / sparse-sharedkv → **HCA** (DeepSeek-V4 alternating layers,
   "Heavily Compressed Attention"; heuristic).
3. else `attention.lightning_indexer` AND `attention.sparse_sharedkv`,
   NO `attention.kv_compressor` → **DSA** (DeepSeek-V3.2,
   "DeepSeek Sparse Attention" per arxiv 2512.02556 §4).
4. else any of `attention.mla.preprocess` /
   `attention.mla.kv_norm_rope_cache` / `attention.mla.v_up_proj`,
   with NO sparse signatures → **MLA** (DeepSeek-V2 / V3).
5. else `attention.linear_or_mamba` present → **linear / mamba / GDN**.
6. else `attention.flash_score` only → **gqa_or_mha** (dense flash-style
   attention umbrella, from kernel categories).

   The underlying kernel (FIA / `UnpadFlashAttention`) supports MHA,
   GQA, AND MQA via `num_key_value_heads` per the CANN docs. From
   kernel categories alone we can't pick between them — hence the
   umbrella label.

   (The same kernel also supports MLA via num_kv=1, but MLA-architected
   layers carry MLA-specific companions like `MlaProlog` /
   `KvRmsNormRopeCache` which are picked up earlier in the decision
   order, so MLA blocks still pin cleanly to `mla`.)

7. **Shape-based refinement (best-effort)** — `rules.refine_dense_attention_from_shapes`:

   After step 6 lands on `gqa_or_mha`, we read
   `kernel_details.csv:Input Shapes` for the FIA / UnpadFA events in
   the block and try to upgrade the label:

   * `num_q_heads == num_kv_heads`                     → `mha`
   * `num_q_heads >  num_kv_heads`, integer ratio       → `gqa`
   * `num_kv_heads == 1` with `num_q_heads > 1`          → `mqa`
   * else (no shapes / sanity-check fail / ambiguous)    → `gqa_or_mha`

   Sanity checks:
   * `head_dim` (last axis) must be in a known-good set
     (sanity check we picked Q/K not e.g. mask / pse).
   * `num_heads` (second-to-last) must be in `[1, 1024]`.
   * Q and K must share `head_dim`.
   * Across multiple FIA events in the same block, the winning kind
     must have outright majority.

   Why this is best-effort, not a contract:
   * CANN profiler may omit Input Shapes on some rows (post-aclgraph
     compile).
   * Tensor order in `input[0..2]` matches the CANN FIA ABI for the
     ops we see today; if vllm-ascend introduces a wrapper that
     reorders inputs, refinement may pick the wrong tensor.
   * Refinement only runs when step 6 lands on `gqa_or_mha`; it never
     overrides MLA / CSA / HCA / DSA / linear decisions.

   The previous `fa` family value was removed for an unrelated
   reason — `UnpadFlashAttention` is the dense long-context branch of
   `AscendAttentionBackend`, NOT a separate FA backend.

Three earlier traps this category-driven decision avoids:
* `UnpadFlashAttention` returning `fa` from a raw-substring matcher
  while the YAML mapped it to the dense path (contract drift).
* The kernel category baking in a GQA-only architecture inference —
  CANN's FIA op explicitly supports MHA / GQA / MLA via
  `num_key_value_heads`. The category was renamed
  `attention.gqa_or_mha → attention.flash_score` and is now neutral.
* A block containing only `KVQuantSparseAttnSharedKVMetadata`
  satisfying the main sparse-shared-KV signature because the substring
  `sharedkv` was loose. The metadata sub-category
  (`attention.sparse_sharedkv.metadata`) is deliberately separate and
  never satisfies the CSA / DSA must-have set.

`attention.kvcomp.topk` is an *overlay* on top of one of the above
(Hamming-distance KV pruning helper) — it does not change the host
family, only appends a `+kvc` suffix to the label.

The sparse-attention kernel categories (`attention.sparse_sharedkv`,
`attention.lightning_indexer`, `attention.kv_compressor`) are kept
**paper-neutral** so the same Compressor kernel can serve both CSA
and HCA without baking the V4 architecture name into the kernel
label.

The label feeds `html_report.detect_attention_subtype` and the L2 block
header.  It does NOT change segmentation or the bound calculation.

## 4. Why we don't split FFN further

Splitting `ffn` into "gate matmul / up matmul / down matmul" would be
purely name-based and breaks for fused kernels (e.g. SwiGLU/SwiGLUQuant,
`GroupedMatmul` for shared experts).  The block-level view already
gives enough resolution for "attention took X ms, FFN/MoE took Y ms";
the per-operator drill-down lives in `block_summary.csv:top_ops` and
`operator_summary.csv`.

If a future taxonomy needs finer granularity, the rule is the same as
the rest of this codebase: declare an explicit rule in
`kernel_signatures.yaml:match_rules` (evaluated by
`rules.categories_and_roles`), surface it as a new `block_kind`
above, and document the boundary rule here -- never grep kernel names
inline.

## 5. Output schema

`block_segments.json`
```json
{
  "block_id": "blk_<hash>",
  "rank_id": "...",
  "segment_id": "seg_<step>",
  "layer_id": "layer_<hash>",
  "layer_index": 17,
  "block_index": 0,
  "block_kind": "attention",
  "companion_layer": false,
  "row_start": 1234,
  "row_end": 1289,
  "start_us": ...,
  "end_us": ...,
  "event_count": 56,
  "block_class_id": "blk_cls_<hash>"
}
```

`block_summary.csv` (per block) carries everything `layer_summary.csv`
carries plus per-block pipeline aggregate (`PIPELINE_FIELDS`),
`bound_stage` / `bound_family` / `dominant_core` (computed on the AIC /
AIV pipeline aggregate, see `bound_classification.md`),
`comm_share` (fraction of wall time spent in HCCL or `mix_comm_aiv`
fused kernels), and `top_ops` (top-5 contributors).

`block_class_summary.csv` rolls multiple blocks of the same class up
into one row (member count, wall mean / p50 / p90, pipeline aggregate
sum, bound classification, comm-share mean, top-10 contributors).
