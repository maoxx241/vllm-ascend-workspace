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

## 4. Why we don't split FFN further

Splitting `ffn` into "gate matmul / up matmul / down matmul" would be
purely name-based and breaks for fused kernels (e.g. SwiGLU/SwiGLUQuant,
`GroupedMatmul` for shared experts).  The block-level view already
gives enough resolution for "attention took X ms, FFN/MoE took Y ms";
the per-operator drill-down lives in `block_summary.csv:top_ops` and
`operator_summary.csv`.

If a future taxonomy needs finer granularity, the rule is the same as
the rest of this codebase: declare an explicit role in
`common.py:categories_and_roles`, surface it as a new `block_kind`
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
