---
name: ascend-operator-debug
description: Reduce an Ascend model-level failure to one torch_npu, ACLNN, or custom operator call, then validate explicit dtype, shape, layout, and eager/compile/graph cases against a reference implementation. Use for operator crashes, unsupported dtype or layout errors, shape-dependent numerical mismatches, or workspace API faults. Do not use for whole-model graph localization, multi-rank failures, performance benchmarking, or profiler analysis.
---

# ascend-operator-debug

Reduce a reproduced failure to one operator and compare its actual outputs against a trusted reference.

Keep dtype, shape, physical layout, strides and eager/compile/graph mode explicit in the business cases. Prefer the smallest input that still reproduces the failure. A passing isolated call supports that call only; a model-level fix needs a model rerun.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/ascend-operator-debug/scripts/operator_debug.py --config operator.json --results case-results.json
```

The config contains operator identity, tolerance and cases. Result files contain observed case metrics or failures. The report computes coverage and classification; absent cases remain inconclusive.

Use ascend-tensor-dump while the first divergent stage is unknown. Use the Triton skills for a Triton candidate.

Read the relevant detail only when needed:


- [Business input example](references/inputs.md)
