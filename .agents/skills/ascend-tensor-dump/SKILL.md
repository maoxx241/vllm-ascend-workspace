---
name: ascend-tensor-dump
description: Capture and compare bounded intermediate tensor dumps on Ascend NPU to find the first stage where numbers diverge. Use when output is wrong, non-finite, or differs between two configurations and the divergence must be localized to a stage, layer, rank, or single operator, in eager or graph mode. Do not use for performance profiling, HBM attribution, debug case bookkeeping, or before a deterministic reproduction with fixed weights and token ids exists.
---

# Ascend tensor dump

Find the first divergent stage using a bounded capture of a deterministic
reproduction. Start with existing output evidence; add instrumentation only when
it can distinguish the current hypothesis. Inputs, sampling and topology should
match the comparison being made.

Use `assets/dump_probe.py` in the actual managed source worktree. Prefer summary
statistics, then capture selected tensors at the first suspicious stage. Retain
logical shape and physical stride, storage offset and layout: summaries alone
can miss aliasing or cache-index errors. Choose a stable request label and an
occurrence within that label when prefill is chunked.

For graph capture, use preallocated `graph_slot()` storage and graph-compatible
copy nodes; read back after replay. Ordinary Python callbacks do not run during
replay. Compare with the probe disabled when synchronization could alter the
symptom.

| Entry | Purpose |
|---|---|
| `scripts/dump_compare.py scan` | Inspect summaries for non-finite stages and storage aliases |
| `scripts/dump_compare.py diff` | Compare corresponding summaries from two captures |
| `scripts/dump_compare.py tensors` | Compare selected tensor values |
| `assets/replay_op.py` | Replay a captured operator invocation against a reference |

Pass actual dump artifacts directly to the relevant comparison; report tools
retain evidence references internally. Missing stages are missing evidence. A
passing operator replay does not establish a whole-model fix. Remove temporary
instrumentation after the investigation and rerun the affected reproduction.

Use graph-debug for compile/capture/replay localization and operator-debug once
the failure is reduced to one operator. Contextual findings are captured through
the normal task summary.

- [Capture and comparison behavior](references/behavior.md)
- [Probe and replay recipes](references/command-recipes.md)
