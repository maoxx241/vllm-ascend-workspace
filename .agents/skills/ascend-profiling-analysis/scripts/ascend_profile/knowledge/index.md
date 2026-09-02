# Knowledge index (read first)

This directory is the **knowledge contract layer** for the
`ascend-profiling-analysis` skill. Read this file first when extending
the skill — it tells you which knowledge files are *active rules* and
which are *reference docs*. Files here are versioned alongside the
analysis code; treat them as part of the contract.

> Status: kernel classification (`kernel_signatures.yaml:match_rules`),
> attention-family resolution (`attention_families.yaml:cheat_sheet.resolver`),
> segmentation layer anchors (`segmentation_rules.yaml`), and diagnosis
> thresholds/wording (`diagnosis_rules.yaml`) are all **loaded at runtime**
> from YAML. Still in Python: `segment.py` strategy, `classify.py` block
> decomposition, and the finding trigger conditions in `diagnostics.py`.
> Schema tests in `.agents/skills/ascend-profiling-analysis/tests/` enforce
> the enum contracts.

## Files in this directory

"Consumed by" below means **loaded as data at runtime**. Files mirrored
by hand in Python (or only cited in comments / report prose) say so
explicitly — do not read a citation as runtime consumption.

| File | Kind | Consumed by | Notes |
|------|------|-------------|-------|
| `index.md` | Reference (this file) | humans / agents | entry point |
| `semantic_conventions.yaml` | **Contract** (active) | `tests/test_semantic_conventions.py` only | stable enum values for `op_type`, `op_roles`, `op_categories`, `bound_family`, `block_kind`, `finding_type`, `alignment_method`, `alignment_confidence`, `html_status`, `report_mode`. Production Python emits the values directly; the YAML is the agent-facing contract layer, not a runtime input |
| `pipeline_taxonomy.md` | Reference | none at runtime — cited in code comments / report prose | AIC / AIV stage mapping from `kernel_details.csv`; coverage policy |
| `bound_classification.md` | Reference | none at runtime — cited in code comments / report prose | how `bound_stage` / `bound_family` / `dominant_core` are derived (decoupled Cube / Vector aware) |
| `step_anatomy.md` | Reference | none at runtime — cited in code comments / report prose | head / main / tail / bubble decomposition for every step |
| `block_taxonomy.md` | Reference | none at runtime — cited in code comments / report prose | attention / ffn / moe block decomposition + companion-layer rule |
| `step_class_grouping.md` | Reference | none at runtime — cited in code comments / report prose | strict shape-equality class signature rules for steps / layers / blocks |
| `communication_taxonomy.md` | Reference | none at runtime — cited in code comments / report prose | HCCL collective op kinds, sub-task primitives (Notify Wait / RDMASend / Memcpy / Reduce_Inline), `mix_comm_aiv` fused kernels, level-0 vs level-1 capture limits |
| `kernel_signatures.yaml` | **Contract** (active) | `rules.categories_and_roles` (loads `match_rules:` at runtime) + `tests/test_kernel_signatures.py` | flat inventory mapping each profile kernel name → category labels + `evidence: path:line` in vllm / vllm-ascend, plus the ordered `match_rules:` rule list that drives classification. Authoritative source when adding a new kernel rule — edit the YAML, the matcher loads it |
| `attention_families.yaml` | **Contract** (active) | `rules.resolve_attention_family` (loads `cheat_sheet.resolver` / `overlay_rule` at runtime) + `tests/test_attention_families.py` | paper-aligned families MLA / DSA / CSA / HCA / GQA / linear / dense. Each family declares the **combination** of category signatures (must_have / must_not_have) that uniquely identifies it on Ascend; the machine-readable decision order lives in `cheat_sheet.resolver` next to the prose cheat-sheet. CANN backend names are documented but never used as family labels |
| `moe_families.yaml` | **Contract** (document-level) | none — mirrored by hand in `tests/test_moe_families.py` (no production consumer) | MC2 / fused MC2 / dense FFN families. **Note:** the `HC*` / `MHC*` prefix kernels (`HCPreSinkhorn`, `HCPreInvRMS`, `HCPost`, `MhcRmsNorm`) are NOT moe.gating sub-kernels — they are **structural block-head helpers** that prefix both attention and MoE blocks and stay under `block_head.mhc_prefix` |
| `segmentation_rules.yaml` | **Contract** (active) | `segment.py:load_segmentation_rules` | attention-family layer-anchor priors: MLA/DSA/CSA layer-start markers + companion-only kernels. Single source of truth for what were the hard-coded `MLA_LAYER_START_CATEGORIES` / `ATTENTION_COMPANION_ONLY_CATEGORIES` constants |
| `diagnosis_rules.yaml` | **Contract** (active) | `diagnostics.py:load_diagnosis_rules` | finding thresholds, severity/confidence, summary templates, and static limitations for every `finding_type`. The trigger *conditions* intentionally stay in `diagnostics.py` (no rule DSL) |
| `hardware_peak_measurements.json` | **Contract** (active) | `hardware_insights.py:load_hardware_measurements` | measured sustained factors per SoC (e.g. Ascend910B4 / A2 32G) used to turn theoretical peaks into attainable denominators |
| `hardware_theoretical_peaks_cann9_0_0.json` | **Contract** (active) | `hardware_insights.py:load_static_theoretical_peaks` | static CANN 9.0.0 theoretical-peak snapshot, used when the analysis host's `platform_config/*.ini` cannot be parsed |
| `model_architectures.yaml` | Reference (document-level) | none — human/agent reference table | HF arch → (attention family, FFN family) high-level map. This skill's input is `ascend_pt/` profiling output — never HF `config.json` — so the file is *not consumed at runtime* by any analysis stage. The report's "model structure" line is derived from observed kernel signatures via `html_report.guess_model_structure`, not from this YAML. Also documents the future `attention_family_mismatch` diagnostic |
| `model_fingerprints.json` | **Contract** (active) | `model_context.py`, `model_insights.py`, `segment.py` (via `model_context`) | concrete model/variant fingerprint catalog. Structural fields must be backed by config evidence or validated profile-visible hints; fuzzy families enumerate variants; `operator_match` rules drive early profile-category model-family matching |
| `model_knowledge_todo.md` | Reference | humans / agents | unverified model facts and missing profile-mode knowledge to collect before promoting into `model_fingerprints.json` |
| `known_counterexamples.md` | Reference | none at runtime — humans / reviewers | concrete profiles that broke segmentation / classification and the invariants future fixes must preserve. Add a case here before changing Python rules |

## Operator taxonomy (canonical list)

The taxonomy maps raw kernels to one or more categories. A single kernel
can have multiple categories. The full inventory lives in
`kernel_signatures.yaml`; `semantic_conventions.yaml` enforces the closed
enum. Headline categories:

- attention (paper-neutral kernel labels; the architecture family —
  `mla`, `dsa`, `csa`, `hca`, `gqa_or_mha` (+ shape-refined
  `mha` / `gqa` / `mqa`), `linear` — is resolved at the report
  layer. The base label comes from the *combination* of categories
  present in one block (see `attention_families.yaml`); the
  refinement of the `gqa_or_mha` umbrella into `mha` / `gqa` /
  `mqa` is a best-effort heuristic over `Input Shapes`, see
  `rules.refine_dense_attention_from_shapes`.
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

## Structure roles

Structure roles describe how categories compose into blocks and layers. The
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

## Diagnosis rules

Diagnosis rules should output claims with evidence and limitations. They should
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

Known counterexamples live in `known_counterexamples.md`; they should be
explicit and testable. For example:

- `argmax` can be sampling/selection, but can also appear in other routing-like
  contexts. It must not be a standalone step boundary.
- Attention-like kernels can represent LLM, VIT, VAE, encoder, or another
  future component. Do not infer semantic component names without supporting
  evidence.

## Adding new knowledge

When adding a new knowledge file, register it in the files table above
and reference it from the analysis stage that consumes it.

1. **New enum value** (e.g. a new `finding_type` or a new `bound_family`):
   add it to `semantic_conventions.yaml` first; `tests/test_semantic_conventions.py`
   then enforces that nothing leaks values outside the enum.
2. **New kernel taxonomy rule** (e.g. a new attention sub-type or new
   MoE fused kernel name):
   1. Add the match rule under `kernel_signatures.yaml:match_rules.rules`
      (ordered; use `unless_category` / `not_predicate` when the rule
      depends on earlier rules) — this is the runtime rule, no Python edit.
   2. Add an inventory entry under `kernel_signatures.yaml:kernels` with
      `evidence: path:line` pointing at the vllm / vllm-ascend source.
      **Anything without evidence is rejected at review.**
   3. If the kernel introduces a new family or changes a family's
      "must-have" set, update `attention_families.yaml` (including
      `cheat_sheet.resolver` when the decision order changes) or
      `moe_families.yaml` accordingly.
   4. Add the new category / role value to `semantic_conventions.yaml`.
   5. `tests/test_kernel_signatures.py` checks the YAML structurally
      (loader validation; every emittable category/role must be a valid
      `semantic_conventions.yaml` enum value — the matcher is YAML-driven,
      so Python↔YAML parity is structural) and behaviourally for a curated
      set of real kernel names — extend the curated cases when you add a
      rule.
3. **New block decomposition variant**: update `block_taxonomy.md`
   first; then `classify.decompose_layer_into_blocks`. Re-run from
   `--from-stage classify`.
4. **New diagnosis rule**: add `finding_type` to
   `semantic_conventions.yaml`, add the thresholds / severity / confidence /
   message template entry to `diagnosis_rules.yaml`, then emit it from
   `diagnostics.py` (conditions stay in Python).
   The evidence-chain validator in `report.py` will reject any finding
   lacking `evidence_ids` / `alignment_ids` / `limitations`.
5. **New known model or architecture fast path**: add the exact/fuzzy model
   entry to `model_fingerprints.json`. Put user-facing aliases under
   `aliases`, concrete variants under `variants`, and profile-category fast
   evidence under `operator_match`. Use generic architecture contexts for
   weak signals such as MoE gating alone; do not promote weak signals to layer
   counts.

Maintenance rules:

- Prefer abstract roles over exact kernel names; store exact names as
  implementation evidence for a role.
- Do not store model size or layer count as core Python logic. Known-model
  structural fields may live in `model_fingerprints.json` only when they are
  backed by `config.json` evidence from an explicit file, Hugging Face,
  ModelScope, or by a validated profile-visible hint.
- Fuzzy family names must enumerate concrete structural variants. For example,
  `dsv4` resolves through Flash/Pro candidates, and `qwen3.5` resolves through
  the known public parameter variants. Quantization and data format suffixes
  are not structural variants; they affect dtype/weight-size analysis only.
- Model-family fast matching belongs in `model_fingerprints.json:operator_match`,
  not in ad hoc segment heuristics. Use strong category combinations where
  possible: `attention.kv_compressor` for DSV4, `attention.lightning_indexer`
  + `attention.sparse_sharedkv` without compressor for DSA, and `moe.gating`
  + `attention.linear_or_mamba` for Qwen3.5/GDN-like hybrids. `moe.gating`
  alone is generic MoE evidence only.
- If a rule uses shape, stream, time, or rank context, record that context in
  the rule name and output evidence.

## Rule-change → stage invalidation

| Change | Re-run from |
|--------|------------|
| operator taxonomy / kernel naming | `--from-stage normalize` |
| segmentation strategy / repair | `--from-stage segment` |
| block taxonomy / attention sub-type | `--from-stage classify` |
| summary metric / bound calc | `--from-stage summarize` |
| diagnosis rules / new finding | `--from-stage diagnostics` |
| report template / HTML widget | `--from-stage report` |

Use `--remote-output-dir <abs-path>` to point the wrapper at a previous
remote run when iterating downstream — that way `normalize` /
`segment` artifacts are reused and only the targeted stage onward is
re-executed.

## Roadmap (deferred to follow-up PRs)

See `references/deferred-work.md` in the skill root. The biggest
remaining "knowledge externalization" items are:

- ~~**YAML-driven matcher**~~ — done: `rules.categories_and_roles` loads
  `kernel_signatures.yaml:match_rules` at runtime, and
  `rules.resolve_attention_family` loads
  `attention_families.yaml:cheat_sheet.resolver`. `moe_families.yaml`
  remains document-level (no production consumer).
- **`segmentation_strategy.yaml`** — remaining anchor priority, boundary
  markers, residual policy, and repair-rule enablement; consumed by
  `segment.py`. (The layer-anchor priors are already externalized in
  `segmentation_rules.yaml`; `segment.py` now records its per-rank
  `segmentation_strategy.mode` as one of `model_guided`,
  `knowledge_uniform_period`, or `exact_cover_knowledge_miss` — the last is
  the explicit knowledge-miss path, not a silent statistical fallback.)
- **`structure_roles.yaml`** — declarative form of the structure-role
  evidence table above.
- **`known_counterexamples.md`** — fixture cases the segmenter /
  classifier must keep passing.
- **`diagnosis_rules.yaml`** — partially done: thresholds, severity,
  confidence, and message templates are externalized and loaded by
  `diagnostics.py`; the trigger conditions stay in Python by design.
  Still open: the `attention_family_mismatch` and
  `block_pattern_unexpected` checks documented in
  `model_architectures.yaml`.
