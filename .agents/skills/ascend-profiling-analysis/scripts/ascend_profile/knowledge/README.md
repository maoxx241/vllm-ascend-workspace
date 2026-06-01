# Knowledge Maintenance

This directory hosts the rule and taxonomy files used by the Ascend
profiling analysis skill.  Files here are versioned alongside the
analysis code; treat them as part of the contract.

## Active files

| File | Topic | Consumed by |
|---|---|---|
| `pipeline_taxonomy.md` | AIC / AIV stage mapping to `kernel_details.csv` columns; coverage policy | `normalize.py`, `summarize.py`, `report.py` |
| `bound_classification.md` | How `bound_stage` / `bound_family` / `dominant_core` are derived (decoupled Cube / Vector aware) | `summarize.py:operator_summary_rows`, `summarize.py:block_summary_rows`, `report.py` |
| `step_anatomy.md` | head / main / tail / bubble decomposition for every step | `summarize.py:step_anatomy_rows`, `report.py` |
| `block_taxonomy.md` | attention / ffn / moe block decomposition + companion-layer rule | `classify.py:decompose_layer_into_blocks`, `summarize.py:block_summary_rows`, `report.py` |
| `step_class_grouping.md` | strict shape-equality class signature rules for steps / layers / blocks | `classify.py:_class_id`, `summarize.py:*_class_summary_rows`, `report.py` |
| `communication_taxonomy.md` | HCCL collective op kinds, sub-task primitives (Notify Wait / RDMASend / Memcpy / Reduce_Inline), `mix_comm_aiv` fused kernels, level-0 vs level-1 capture limits | `summarize.py:hccl_op_summary_rows`, `summarize.py:operator_class_summary_rows`, `report.py`, `cross_rank.py` |
| `kernel_signatures.yaml` | Flat inventory: each profile kernel name → category labels + `evidence: path:line` in vllm / vllm-ascend. Authoritative source when adding a new kernel rule. | `common.categories_and_roles`, `tests/test_kernel_signatures.py` |
| `attention_families.yaml` | MLA / SFA / KVComp / linear / dense families. Each family declares the **combination** of category signatures (must_have / must_not_have) that uniquely identifies it on Ascend; SFA is the in-code name for DeepSeek-V3.2 / V4 sparse attention (NOT "NSA" / "CSA"). | `common.categories_and_roles`, `html_report.detect_attention_subtype`, `tests/test_attention_families.py` |
| `moe_families.yaml` | MC2 / fused MC2 / dense FFN families. Also explicitly documents that the HC* / MHC* prefix kernels (`HCPreSinkhorn`, `HCPreInvRMS`, `HCPost`, `MhcRmsNorm`) are **structural block-head helpers** that appear before BOTH attention and MoE blocks — they stay under `block_head.mhc_prefix` and must NOT be conflated with `moe.gating`. | `common.categories_and_roles`, `tests/test_moe_families.py` |
| `model_architectures.yaml` | HF arch → (attention family, FFN family) high-level map. **Static knowledge / documentation only.** This skill's input is `ascend_pt/` profiling output — never HF `config.json` — so the file is *not consumed at runtime* by any analysis stage. The report's "model structure" line (e.g. `27L · mla+moe`) is derived from observed kernel signatures via `html_report.guess_model_structure`, not from this YAML. Use it as a human reference table when reasoning about which kernel signatures *should* be seen for each architecture. | (none — documentation) |

| `known_counterexamples.md` | Concrete profiling patterns that previously broke segmentation / classification. Add a case here before changing Python rules. | `segment.py`, `classify.py`, reviewer audit |

When adding a new knowledge file, register it in the table above and
reference it from the analysis stage that consumes it.

The maintenance rule is simple:

- Prefer abstract roles over exact kernel names.
- Store exact names as implementation evidence for a role.
- Do not store model size or layer count as core logic.
- If a rule uses shape, stream, time, or rank context, record that context in
  the rule name and output evidence.

## Suggested Files (still to come)

```text
structure_roles.yaml
diagnosis_rules.yaml
```

## Operator Taxonomy (canonical list)

The taxonomy maps raw kernels to one or more categories.  A single kernel
can have multiple categories.  The full inventory lives in
`kernel_signatures.yaml`; `semantic_conventions.yaml` enforces the closed
enum.  Headline categories:

- attention (paper-neutral kernel labels; the architecture family —
  `mla`, `dsa`, `csa`, `hca`, `gqa_or_mha` (+ shape-refined
  `mha` / `gqa` / `mqa`), `linear` — is resolved at the report
  layer. The base label comes from the *combination* of categories
  present in one block (see `attention_families.yaml`); the
  refinement of the `gqa_or_mha` umbrella into `mha` / `gqa` /
  `mqa` is a best-effort heuristic over `Input Shapes`, see
  `common.refine_dense_attention_from_shapes`.
  - `attention.flash_score`          — dense flash-style score kernel
                                       (`FusedInferAttentionScore[V*]`,
                                       `UnpadFlashAttention`). Neutral
                                       on purpose — per the CANN docs
                                       these ops support MHA / GQA / MLA
                                       via ``num_key_value_heads``;
                                       architecture inference is the
                                       resolver's job, not the kernel
                                       label's.
  - `attention.mla`                  — MLA preprocess / decode marker (DSV2/V3, also reused by DSA)
  - `attention.mla.kv_norm_rope_cache` — `KvRmsNormRopeCache` fused op
  - `attention.mla.preprocess`       — `MlaProlog` / `MlaPrologV2` / `MlaPreprocess` (CANN canonical names)
  - `attention.mla.v_up_proj`        — MLA V up-projection BMM
  - `attention.sparse_sharedkv`      — main sparse attention kernel (`KVQuantSparseAttnSharedKV`),
                                       shared by DSA (V3.2) and CSA (V4) — family is resolved by
                                       whether `attention.kv_compressor` is also present
  - `attention.sparse_sharedkv.metadata` — metadata sub-kernel of the above
  - `attention.lightning_indexer`    — `LightningIndexer` (top-k token/block selector)
  - `attention.kv_compressor`        — `Compressor` / `KVCompressEpilog`; **only** V4 (CSA / HCA)
  - `attention.sparse_attn.v_up_proj` — SFA-side V up-projection BMM
  - `attention.kvcomp.topk`          — `NpuHammingDistTopK` decode overlay
  - `attention.kvcomp.signpack`      — sign-bit packing helper
  - `attention.kvcomp.cache_write`   — `NpuReshapeAndCacheBnsd`
  - `attention.linear_or_mamba`      — Mamba / GDN / linear-attn kernels
  - `attention.rope.*`               — RoPE variants (interleave, partial, indexed)
- moe
  - `moe.gating`                     — top-k selection (the genuine `MoeGatingTopK*` op only;
                                       HC*/MHC* prefix kernels do NOT belong here — they are
                                       block-head structural helpers that prefix BOTH attention
                                       and MoE blocks)
  - `moe.dispatch`
  - `moe.combine`
  - `moe.dispatch_expert_compute`    — fused MC2 single-kernel path
  - `moe.expert_matmul`              — `GroupedMatmul` and variants
- compute
  - `compute.matmul`
  - `compute.aux`
- quant
  - `quant.dynamic`
  - `quant.mx`
  - `quant.matmul`
- communication
  - `communication.collective`
  - `communication.allreduce` / `.allgather` / `.reducescatter` / `.alltoallv`
- sampling
  - `sampling.argmax`
  - `sampling.top_k_top_p`
  - `sampling_or_selection`
- system
  - `normalization`
  - `block_head`
  - `aicpu`
  - `dummy_or_reduced_work`

Two earlier drafts coined non-canonical names: `attention.csa*` (used
as a generic catch-all) and `attention.sfa*` (used after a wrong
subagent reading). **Neither is used anymore.** Sparse-attention
kernels now live under the paper-neutral names listed above; the
paper-aligned architecture family (`mla` / `dsa` / `csa` / `hca` /
`gqa_or_mha`) is resolved at the report layer, never baked into the kernel
category. See `kernel_signatures.yaml:deprecated_categories` for the
migration map.

## Structure Roles

Structure roles describe how categories compose into blocks and layers.  The
same role can be proven by different implementation evidence.

Examples:

- `gqa_or_mha_attention_block` (umbrella; report may render as
  `mha` / `gqa` / `mqa` when shape-refinement succeeds)
  - accepted evidence: `attention.flash_score` only (no MLA / sparse markers)
  - shape refinement: if FIA / UnpadFA `Input Shapes` are available and
    Q/K head counts pass sanity checks, the block is reported with the
    shape-refined sub-kind. Falls back to `gqa_or_mha` when shapes are
    missing or ambiguous.
- `moe_block`
  - accepted evidence: `moe.gating` plus one of `moe.dispatch_expert_compute`,
    `moe.dispatch + compute.matmul + moe.combine`
- `csa_attention_block`  (DeepSeek-V4 main layers — Compressed Sparse Attention)
  - accepted evidence: `attention.kv_compressor` + `attention.lightning_indexer`
    + `attention.sparse_sharedkv` together in one block. MLA companions
    (`attention.mla.kv_norm_rope_cache`, `attention.mla.preprocess`) may also
    appear because the SFA backend reuses MLAPO at small token counts.
- `hca_attention_block`  (DeepSeek-V4 alternating layers — Heavily Compressed Attention; heuristic)
  - accepted evidence: `attention.kv_compressor` + `attention.flash_score`
    with NO `attention.lightning_indexer` and NO `attention.sparse_sharedkv`.
- `dsa_attention_block`  (DeepSeek-V3.2 — DeepSeek Sparse Attention per arxiv 2512.02556)
  - accepted evidence: `attention.lightning_indexer` + `attention.sparse_sharedkv`
    with NO `attention.kv_compressor`. MLA companions are expected because
    DSA is built on MLA in MQA mode (paper §4).
- `mla_attention_block`  (DeepSeek-V2 / V3 — Multi-head Latent Attention)
  - accepted evidence: `attention.mla.kv_norm_rope_cache` plus
    `attention.flash_score` (FIA still computes the MLA decode score,
    invoked with ``num_key_value_heads = 1``), without ANY
    sparse-attention signature (`attention.kv_compressor`,
    `attention.lightning_indexer`, `attention.sparse_sharedkv`).
- `block_head`
  - accepted evidence: add+norm, fused add-norm, MHC+norm, or fused
    communication/matmul/add/norm prefix

## Diagnosis Rules

Diagnosis rules should output claims with evidence and limitations.  They should
not directly write prose.

Examples:

- `communication_collective_slow`
  - evidence: same collective op aligned across ranks, similar launch time,
    slow common completion or long duration distribution.
- `ep_load_imbalance_suspected`
  - evidence: alltoallv or dispatch/combine duration skew across ranks.
- `slow_rank_suspected`
  - evidence: similar matmul shape, large start skew, communication launch skew,
    or abnormal dispatchffncombine duration.
- `dp_workload_imbalance`
  - evidence: large T-axis or token-shape difference across DP ranks.
- `reduced_work_or_dummy_rank`
  - evidence: same time window, one rank has full workload structure and another
    lacks the attention/body structure.
- `rank_workload_asymmetry`
  - evidence: a complete structure appears on one rank but not others.

## Counterexamples

Known counterexamples should be explicit and testable.  For example:

- `argmax` can be sampling/selection, but can also appear in other routing-like
  contexts.  It must not be a standalone step boundary.
- Attention-like kernels can represent LLM, VIT, VAE, encoder, or another
  future component.  Do not infer semantic component names without supporting
  evidence.

