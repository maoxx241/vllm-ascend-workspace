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

When adding a new knowledge file, register it in the table above and
reference it from the analysis stage that consumes it.

The maintenance rule is simple:

- Prefer abstract roles over exact kernel names.
- Store exact names as implementation evidence for a role.
- Do not store model size or layer count as core logic.
- If a rule uses shape, stream, time, or rank context, record that context in
  the rule name and output evidence.

## Suggested Files

```text
operator_taxonomy.yaml
structure_roles.yaml
diagnosis_rules.yaml
known_counterexamples.yaml
```

## Operator Taxonomy

The taxonomy should map raw kernels to one or more categories.  A single kernel
can have multiple categories.

Example categories:

- `attention.gqa_or_mha`
- `attention.mla`
- `attention.csa`
- `attention.linear_or_mamba`
- `moe.gating`
- `moe.dispatch`
- `moe.combine`
- `moe.dispatch_expert_compute`
- `compute.matmul`
- `communication.collective`
- `block_head`
- `sampling_or_selection`
- `aicpu`
- `dummy_or_reduced_work`

## Structure Roles

Structure roles describe how categories compose into blocks and layers.  The
same role can be proven by different implementation evidence.

Examples:

- `gqa_attention_block`
  - accepted evidence: `attention.gqa_or_mha`
- `moe_block`
  - accepted evidence: `moe.gating` plus one of `moe.dispatch_expert_compute`,
    `moe.dispatch + compute.matmul + moe.combine`
- `csa_attention_block`
  - accepted evidence: CSA sparse attention, optionally compressor/indexer
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

