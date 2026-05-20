# Ascend Profiling Analysis Framework

This directory defines the stable data model for the profiling analysis skill.
The goal is accuracy and traceability first.  Implementation scripts may change,
but the artifacts here should stay readable by agents and auditable by humans.

## Design Contract

The framework is split into four layers:

1. `normalized_event_index`
   - Converts raw profiling files into timestamped events.
   - Preserves source row references to the original files.
   - Does not infer model semantics.

2. `structure_evidence_graph`
   - Maps events into abstract roles such as `attention`, `moe_dispatch`,
     `block_head`, `sampling_or_selection`, `communication`, and `dummy_work`.
   - Kernel names are evidence, not final structure definitions.
   - Multiple kernel implementations can prove the same structure.

3. `diagnosis_evidence_tables`
   - Produces per-rank and cross-rank findings from structured evidence.
   - Cross-rank analysis compares and aligns ranks.  It must not rewrite
     single-rank step boundaries.

4. `report_package`
   - Produces `report.md`, `report.xlsx`, and `manifest.json`.
   - Markdown explains conclusions and limitations.
   - XLSX keeps complete sortable/calculable tables.
   - Manifest binds every report claim to evidence and raw source rows.

## Naming

Avoid version-numbered primary entry points for new code.  Prefer responsibility
names:

- `normalize.py`
- `segment.py`
- `summarize.py`
- `cross_rank.py`
- `diagnostics.py`
- `report.py`

This package is the only supported profiling analysis entry point.  Historical
`split_kernel_details_steps*`, `analyze_step_segments*`, `sweep_step_segments*`,
and old case/spec files were removed after full remote regression passed.

## Current CLI

Run the full pipeline:

```bash
python3 tools/ascend_profile/analyze.py PROFILE_ROOT --output OUT_DIR --verbose
```

Run individual stages for debugging:

```bash
python3 tools/ascend_profile/normalize.py PROFILE_ROOT --output OUT_DIR
python3 tools/ascend_profile/segment.py --output OUT_DIR
python3 tools/ascend_profile/summarize.py --output OUT_DIR
python3 tools/ascend_profile/cross_rank.py --output OUT_DIR
python3 tools/ascend_profile/diagnostics.py --output OUT_DIR
python3 tools/ascend_profile/report.py --output OUT_DIR
```

The staged commands are intended for agent debugging.  The skill entrypoint
should call `analyze.py` unless it needs to inspect an intermediate artifact.

## Remote Execution Rule

Do not parse large profiling roots on the local Mac.  Local execution is limited
to static checks and small schema validation.  Real profiling analysis should
run inside the remote container that has the profiling data, for example:

```bash
ssh -p 46000 root@173.131.1.2 \
  'cd /tmp/ascend_profile_framework && \
   python3 -m tools.ascend_profile.analyze PROFILE_ROOT --output OUT_DIR'
```

## Required Traceability

Every derived artifact must keep enough information to reconstruct the evidence
chain:

```text
report claim
  -> diagnosis finding
  -> evidence refs
  -> event / segment / alignment ids
  -> source file path + row range
  -> original kernel_details.csv / trace_view.json / op_summary / communication.json
```

If a claim cannot be backed by row-level or event-level evidence, it must be
reported as a limitation instead of a conclusion.

## Artifact Layout

Recommended output for one profiling root:

```text
profile_analysis/
  manifest.json
  source_index.json
  normalize_manifest.json
  segment_manifest.json
  summary_manifest.json
  cross_rank_manifest.json
  normalized_event_index.jsonl
  normalized_event_index.csv
  step_segments.json
  layer_segments.json
  structure_evidence_graph.json
  rank_summary.csv
  step_summary.csv
  layer_summary.csv
  operator_summary.csv
  wait_anchor_ops.csv
  aicpu_summary.csv
  cross_rank_alignment.csv
  cross_rank_alignment.json
  diagnosis_findings.json
  report/
    report.md
    report.xlsx
    manifest.json
  evidence/
    bubble_windows.jsonl
  evidence_index.csv
  raw_kernel_index.csv
```

`normalized_event_index.jsonl` is optional for stage-level debugging.  The full
pipeline always emits `normalized_event_index.csv`, and downstream stages can
load that CSV directly.

The CSV/XLSX tables are user-facing and spreadsheet-friendly.  The JSON/JSONL
files are agent-facing and preserve nested evidence.

## Core Concepts

### SourceRef

Identifies raw or materialized data:

- `source_id`
- `path`
- `kind`
- `sha256`
- `row_start`
- `row_end`
- `row_base`

### EvidenceRef

Evidence is reusable.  A finding should reference evidence ids instead of
embedding large raw snippets everywhere.

Typical evidence kinds:

- `raw_kernel_rows`
- `step_window`
- `layer_window`
- `operator_group`
- `bubble_window`
- `cross_rank_op_alignment`
- `cross_rank_step_alignment`
- `shape_signature_match`
- `counter_evidence`

### Claim

A claim is an auditable statement:

- `claim_id`
- `claim_type`
- `summary`
- `confidence`
- `severity`
- `evidence_ids`
- `counter_evidence_ids`
- `limitations`

Reports should be assembled from claims, not from hidden logic inside Markdown
rendering.

## Structure Role Abstraction

The taxonomy should map implementation-specific kernels into abstract roles.
Examples:

| Raw implementation evidence | Abstract role |
|---|---|
| `FusedInferAttentionScore`, `UnpadFlashAttention`, flash attention variants | `attention.gqa_or_mha` |
| MLA-like attention kernels | `attention.mla` |
| CSA/sparse sharedkv/indexer/compressor evidence | `attention.csa` |
| `causal_conv1d`, mamba/GDN hints | `attention.linear_or_mamba` |
| `dispatch + combine`, `dispatchffncombine`, alltoallv + expert matmul | `moe.dispatch_expert_compute` |
| `add + norm`, fused add-norm, MHC + norm, fused matmul-allreduce-add-norm | `block_head` |
| `argmax` with sampling context | `sampling_or_selection` |
| HCCL/HCOM/allreduce/allgather/reducescatter/alltoall | `communication.collective` |

This lets future kernels prove known structures without hardcoding model sizes
or exact model names.

## Cross-Rank Analysis Principles

Cross-rank logic should create evidence tables, not mutate rank-local segments.

Required cross-rank views:

1. `time_window_alignment`
   - Aligns events and steps by overlap on the global device timeline.

2. `structure_alignment`
   - Aligns steps/layers by structure signature and role inventory.

3. `operator_alignment`
   - Aligns communication ops, matmuls, dispatch/combine, attention, and
     selection ops across ranks.

4. `shape_alignment`
   - Compares shape signatures.  Near matches should record tolerated dimensions
     explicitly, for example token dimension difference of 1 or 2.

Cross-rank findings should be generic:

- `communication_collective_slow`
- `ep_load_imbalance_suspected`
- `slow_rank_suspected`
- `dp_workload_imbalance`
- `reduced_work_or_dummy_rank`
- `rank_workload_asymmetry`

Do not hardcode findings as `vit_vs_llm`.  A rank workload asymmetry may be VIT,
VAE, encoder, decode, prefill, dummy work, or a future component.

## Confidence Policy

Use layered certainty:

- `high`: direct row/event evidence and consistent cross-checks.
- `medium`: direct evidence exists but one expected corroborating source is
  missing.
- `low`: pattern is suspicious but source coverage is incomplete.

Never hide an anomaly because the root cause is unclear.  Emit the anomaly with
`insufficient_evidence` or a low-confidence claim.

## Schema

The schema draft lives in `schemas/analysis_bundle.schema.json`.  It is designed
to be permissive enough for iteration while keeping core ids and evidence fields
stable.

## Agent skill

The remote-orchestration wrapper for this framework lives under
`.agents/skills/ascend-profiling-analysis/`.  Use that skill's `SKILL.md`
for the agent-facing entry points, failure policy, and output contract;
this README defines the underlying framework's data contract only.
