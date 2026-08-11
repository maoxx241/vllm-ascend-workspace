---
name: ascend-triton-kernel-optimization
description: Profile and iteratively optimize a correctness-passed Ascend Triton kernel with explicit NPU baselines, per-shape measurements, UB live-set and physical-core reasoning, MTE/Vector/Scalar bottleneck attribution, one-hypothesis rounds, noise-aware KEEP/DISCARD decisions, and Run Manifest evidence. Use for single-kernel latency or throughput improvement after all planned correctness cases pass. Do not use to create or migrate the first correct kernel, bypass failed validation, assess whole-model serving regressions, attribute model HBM, or diagnose a non-Triton operator.
---

# Ascend Triton Kernel Optimization

Optimize one correct kernel against a comparable NPU baseline. Preserve correctness and evidence integrity at every round.

## Workflow

1. Require a passed `ascend-triton-kernel-validation` manifest for the exact candidate hash and case set.
2. Record target SoC/software versions, NPU baseline, warmup/repeat policy, per-case medians, noise threshold, case-regression limit, and performance objective.
3. Query `.agents/knowledge/` with current profiler signals and failure signatures before selecting an optimization.
4. Run `scripts/triton_optimization.py plan` to lock the baseline, objective, cases, and starting kernel.
5. Use `msprof op` or the target version's supported operator profiler to compare device time, block count, MTE2/MTE3, Vector, Scalar/FLOWCTRL, and pipeline gaps against theoretical floors.
6. Form one falsifiable hypothesis, change one primary mechanism, and rerun the full correctness gate on the candidate.
7. Normalize per-case measurements and run `record`. The controller chooses `KEEP`, `DISCARD`, `NOISE`, or `FAIL`; never overwrite the best kernel.
8. After repeated failures, enter diagnosis and change the bottleneck model rather than retrying the same parameter.
9. Run `analyze` only when the objective is met, round budget is exhausted, or the run intentionally stops.

## Entry point

`scripts/triton_optimization.py` provides:

- `plan`: validate correctness evidence and lock baseline/configuration;
- `record`: enforce sequential rounds, parent hash, full-case validation, measurement coverage, and noise-aware decisions;
- `analyze`: report best kernel, cumulative improvement, discarded hypotheses, and terminal objective status.

Read:

- [Behavior contract](references/behavior.md) for config, round, decision, and status semantics.
- [Profiling decision tree](references/profiling-decision-tree.md) before choosing a hypothesis.
- [Ascend optimization techniques](references/ascend-techniques.md) only for the diagnosed bottleneck.
- [Command recipes](references/command-recipes.md) for the lifecycle.
- [Acceptance](references/acceptance.md) before claiming an optimization.

## Rules

- Never benchmark a failed or inconclusive correctness candidate.
- Compare like-for-like NPU states; GPU latency is context, not the only NPU acceptance baseline.
- Exclude compile and warmup, preserve raw values, and remeasure noise before small decisions.
- Treat core count, UB capacity, alignment, dtype support, and compiler options as target-specific capabilities.
- Keep run state under `.vaws-local/ascend-triton/optimization/`.
