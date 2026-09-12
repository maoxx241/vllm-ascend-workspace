---
name: vllm-ascend-performance-regression
description: Plan, record, and analyze controlled baseline-versus-candidate vLLM Ascend serving performance experiments with isolated sessions, identical non-code configuration, alternating A/B order, warmup exclusion, variance and outlier reporting, and metric-specific regression thresholds. Use for throughput, TTFT, TPOT, ITL, acceptance-rate, startup-time, or HBM regression checks. Do not use for correctness, single-state measurement, HBM component attribution, or profiling root-cause analysis.
---

# vllm-ascend-performance-regression

Run or analyze a controlled baseline-versus-candidate serving experiment and apply metric-specific regression thresholds.

Choose the workload and metrics that reflect the user-visible change. Set threshold and direction per metric. Keep non-code conditions comparable and inspect variance, outliers and failed requests before attributing a delta to code.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py --config experiment.json
```

The business config names baseline.sources and candidate.sources (actual vllm and vllm-ascend worktrees), benchmark options, runs, warmups and thresholds. The collector passes each source selection to that run without changing task defaults, waits for its managed service, warms each launch, alternates A/B order, records runtime observations, and releases owned executions. --results accepts existing measurement files for report-only use. Missing runtime evidence yields an inconclusive report.

For a single-state throughput measurement use benchmark. For root-cause timing attribution use profiling collection and analysis.

Read the relevant detail only when needed:


- [Experiment input example](references/inputs.md)
