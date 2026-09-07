---
name: vllm-ascend-performance-regression
description: Plan, record, and analyze controlled baseline-versus-candidate vLLM Ascend serving performance experiments with isolated sessions, identical non-code configuration, alternating A/B order, warmup exclusion, variance and outlier reporting, and metric-specific regression thresholds. Use for throughput, TTFT, TPOT, ITL, acceptance-rate, startup-time, or HBM regression checks. Do not use for correctness, single-state measurement, HBM component attribution, or profiling root-cause analysis.
---

# vLLM Ascend Performance Regression

Wrap `vllm-ascend-benchmark` with a controlled two-state experiment.

## Workflow

1. Create independent baseline and candidate worktrees and `session-management` sessions.
2. Use the same machine allocation policy, NPU count, model and weight hash, environment, topology, Serving arguments, Benchmark arguments, dataset, request rate, and concurrency.
3. Put all non-code conditions in the experiment `shared` object. `plan` requires `machine`, `npu_devices`, `model`, `environment`, `topology` (with `tp` and `dp`), `serve_args`, `bench_args`, `dataset`, `max_concurrency`, and `request_rate`; a `shared` object without them cannot produce a parity certificate.
4. Run `scripts/performance_regression.py plan`; set `parent_run_id` in the config when the experiment is evidence for a change-validation plan.
5. Follow `schedule.json` exactly. Before each state executes, establish `remote-code-parity`, start or confirm its service, then call `vllm-ascend-benchmark`.
6. Normalize each raw Benchmark result with `normalize`, then call `record`.
7. Run `analyze` only after the schedule is complete.
8. If the result is failed or inconclusive and operator timing is needed, recommend profiling collection; do not collect heavy profiles automatically.

For three measurements the alternating sequence is:

```text
baseline warmup
candidate warmup
baseline 1
candidate 1
candidate 2
baseline 2
baseline 3
candidate 3
```

## Entry point

`scripts/performance_regression.py` provides:

- `plan`: validate that `shared` declares every parity condition, generate the alternating schedule, write a `parity-check.json` that names what it did and did not verify, and create Run Manifest v1;
- `normalize`: convert one single-run or aggregated Benchmark result into the measurement contract;
- `record`: accept the next normalized measurement only when its state, phase, ordinal, and config hash match the schedule;
- `analyze`: consume an observational comparability certificate built from every measure-phase observation, then exclude warmups, report mean, sample deviation, coefficient of variation, outliers, relative change, and threshold verdict. `passed` requires the certificate.

Read:

- [Behavior contract](references/behavior.md) for config, schedule, measurement, statistics, and status semantics.
- [Command recipes](references/command-recipes.md) for the full lifecycle.
- [Acceptance](references/acceptance.md) before claiming a regression or pass.

## Rules

- Never compare measurements with different config hashes.
- Never run all baseline measurements before all candidate measurements.
- Do not include warmups in statistics.
- Preserve raw values even when configured to exclude detected outliers from the decision set.
- Return `inconclusive` when required metrics are missing, too few decision values remain, or observed variation exceeds `max_cv`.
- Use metric direction explicitly: higher is better or lower is better.
- Keep experiment state under `.vaws-local/performance-regression/`.
