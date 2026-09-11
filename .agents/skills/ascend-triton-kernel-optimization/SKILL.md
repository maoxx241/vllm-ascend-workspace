---
name: ascend-triton-kernel-optimization
description: Profile and iteratively optimize a correctness-passed Ascend Triton kernel with explicit NPU baselines, per-shape measurements, UB live-set and physical-core reasoning, MTE/Vector/Scalar bottleneck attribution, one-hypothesis rounds, noise-aware KEEP/DISCARD decisions, and Run Manifest evidence. Use for single-kernel latency or throughput improvement after all planned correctness cases pass. Do not use to create or migrate the first correct kernel, bypass failed validation, assess whole-model serving regressions, attribute model HBM, or diagnose a non-Triton operator.
---

# ascend-triton-kernel-optimization

Optimize an already validated Ascend Triton kernel using measured bottlenecks and repeatable latency evidence.

Choose one bottleneck hypothesis per round. Consider UB live set, physical cores and MTE/Vector/Scalar overlap. Compare repeated per-shape measurements with a reference baseline; retain a change only when the gain exceeds noise and correctness still covers the candidate.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py --config optimization.json --results round-results.json
```

The config contains op_name, kernel and its validation evidence, target, cases, baseline measurements and objective. Round results carry candidate measurements and validation. The report computes KEEP/DISCARD and verifies kernel lineage and case coverage.

Use ascend-triton-kernel-validation when correctness is incomplete. Use profiling-analysis for whole-model performance attribution.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
- [profiling decision tree](references/profiling-decision-tree.md)
- [ascend techniques](references/ascend-techniques.md)
