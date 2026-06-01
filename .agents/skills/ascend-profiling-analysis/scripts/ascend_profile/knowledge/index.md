# Knowledge index (read first)

This directory is the **knowledge contract layer** for the
`ascend-profiling-analysis` skill. Read this file first when extending
the skill — it tells you which knowledge files are *active rules* and
which are *reference docs*.

> Status: most active rules still live in Python (see "Roadmap" below).
> The YAML / Markdown files below are the canonical *contract* that
> Python must keep in sync with. Schema tests in
> `.agents/skills/ascend-profiling-analysis/tests/` enforce a subset.

## Files in this directory

| File | Kind | Consumed by | Notes |
|------|------|-------------|-------|
| `index.md` | Reference (this file) | humans / agents | entry point |
| `semantic_conventions.yaml` | **Contract** (active) | `tests/test_semantic_conventions.py` + downstream consumers | stable enum values for `op_type`, `op_roles`, `op_categories`, `bound_family`, `block_kind`, `finding_type`, `alignment_method`, `alignment_confidence`, `html_status`, `report_mode` |
| `pipeline_taxonomy.md` | Reference | `normalize.py`, `summarize.py`, `report.py` | AIC / AIV stage mapping from `kernel_details.csv` |
| `bound_classification.md` | Reference | `summarize.py`, `report.py` | how `bound_stage` / `bound_family` are derived |
| `step_anatomy.md` | Reference | `summarize.py:step_anatomy_rows`, `report.py` | head / main / tail / bubble |
| `block_taxonomy.md` | Reference | `classify.py:decompose_layer_into_blocks`, `summarize.py:block_summary_rows`, `report.py` | attention / ffn / moe / aicpu |
| `step_class_grouping.md` | Reference | `classify.py:_class_id`, `summarize.py:*_class_summary_rows` | strict shape-equality class signature |
| `communication_taxonomy.md` | Reference | `summarize.py`, `report.py`, `cross_rank.py` | HCCL collectives + `mix_comm_aiv` |
| `kernel_signatures.yaml` | **Contract** (active reference, Python mirrors it) | `common.categories_and_roles`, `tests/test_kernel_signatures.py` | flat inventory mapping each profile kernel name → category labels + evidence path:line in vllm / vllm-ascend |
| `attention_families.yaml` | **Contract** (active reference) | `common.categories_and_roles`, `html_report.detect_attention_subtype`, `tests/test_attention_families.py` | paper-aligned families MLA / DSA / CSA / HCA / GQA / linear / FA, with "must-have / must-not-have" signature combinations; CANN backend names are documented but never used as family labels |
| `moe_families.yaml` | **Contract** (active reference) | `common.categories_and_roles`, `tests/test_moe_families.py` | MC2 / fused MC2 / dense FFN families. **Note:** the `HC*` / `MHC*` prefix kernels are NOT moe.gating sub-kernels — they prefix both attention and MoE blocks and stay under `block_head.mhc_prefix` |
| `model_architectures.yaml` | Reference (report-time annotation only) | future diagnostics for `attention_family_mismatch` | HF arch → (attention family, FFN family); NOT used for segmentation |
| `known_counterexamples.md` | Reference | `segment.py`, `classify.py`, reviewers | concrete profiles that broke segmentation / classification and the invariants future fixes must preserve |
| `README.md` | Reference | humans | historical notes; roadmap |

## Adding new knowledge

1. **New enum value** (e.g. a new `finding_type` or a new `bound_family`):
   add it to `semantic_conventions.yaml` first; `tests/test_semantic_conventions.py`
   then enforces that nothing leaks values outside the enum.
2. **New kernel taxonomy rule** (e.g. a new attention sub-type or new
   MoE fused kernel name):
   1. Add an entry to `kernel_signatures.yaml` with `evidence: path:line`
      pointing at the vllm / vllm-ascend source. **Anything without
      evidence is rejected at review.**
   2. If the kernel introduces a new family or changes a family's
      "must-have" set, update `attention_families.yaml` or
      `moe_families.yaml` accordingly.
   3. Mirror the rule in `common.categories_and_roles()` (Python still
      runs the matcher today; YAML is the contract).
   4. Add the new category / role value to `semantic_conventions.yaml`.
   5. `tests/test_kernel_signatures.py` rejects any category emitted by
      Python that is missing from the YAML inventory.
3. **New block decomposition variant**: update `block_taxonomy.md`
   first; then `classify.decompose_layer_into_blocks`. Re-run from
   `--from-stage classify`.
4. **New diagnosis rule**: add `finding_type` to
   `semantic_conventions.yaml`, then emit it from `diagnostics.py`.
   The evidence-chain validator in `report.py` will reject any finding
   lacking `evidence_ids` / `alignment_ids` / `limitations`.

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

- **YAML-driven matcher** — replace the Python rule list in
  `common.categories_and_roles()` with a loader that reads
  `kernel_signatures.yaml` + `attention_families.yaml` +
  `moe_families.yaml` directly. Today Python mirrors the YAML by hand
  and the schema test enforces parity.
- **`segmentation_strategy.yaml`** — anchor priority, boundary markers,
  residual policy, repair-rule enablement; consumed by `segment.py`.
- **`known_counterexamples.md`** — fixture cases the segmenter /
  classifier must keep passing.
- **`diagnosis_rules.yaml`** — declarative rule pack for
  `diagnostics.py`, including the `attention_family_mismatch` and
  `block_pattern_unexpected` checks documented in
  `model_architectures.yaml`.
