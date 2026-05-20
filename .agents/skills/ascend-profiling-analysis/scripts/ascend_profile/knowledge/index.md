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
| `README.md` | Reference | humans | historical notes; roadmap |

## Adding new knowledge

1. **New enum value** (e.g. a new `finding_type` or a new `bound_family`):
   add it to `semantic_conventions.yaml` first; `tests/test_semantic_conventions.py`
   then enforces that nothing leaks values outside the enum.
2. **New kernel taxonomy rule** (e.g. a new attention sub-type or new
   MoE fused kernel name): start with a counterexample in
   `known_counterexamples.md` (create the file if missing), then update
   `common.categories_and_roles()` to match it. After the rule landed,
   add the new role / category value to `semantic_conventions.yaml`.
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

- **`operator_taxonomy.yaml`** — replace the Python rule list in
  `common.categories_and_roles()` with a YAML-driven matcher.
- **`segmentation_strategy.yaml`** — anchor priority, boundary markers,
  residual policy, repair-rule enablement; consumed by `segment.py`.
- **`known_counterexamples.yaml`** — fixture cases the segmenter /
  classifier must keep passing.
- **`diagnosis_rules.yaml`** — declarative rule pack for
  `diagnostics.py`.
