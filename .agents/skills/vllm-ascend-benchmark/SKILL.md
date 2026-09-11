---
name: vllm-ascend-benchmark
description: Run vLLM online-serving benchmarks on a workspace-managed remote container. Use for requests like "跑个 benchmark", "对比性能", "压测一下", "测下吞吐", or "看下有没有性能回退". Do not use for accuracy tests, nightly CI matrix runs, offline inference, or service-only lifecycle.
---

# vllm-ascend-benchmark

Measure a vLLM service with one or several benchmark iterations and return raw and normalized metrics.

Choose input/output lengths, concurrency, request rate and endpoint for the intended workload. User choices override presets and nightly examples. Report variance and failures alongside throughput and latency.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py --model /models/example --runs 3 --warmup-runs 1
```

Use --execution-id to measure an existing service, or let the workflow start and clean up its own service. --serve-args and --bench-args forward business options; --preset supplies reusable defaults. The managed interpreter and actual launch observations are recorded with measurements.

Use performance-regression for code comparisons: it binds actual local worktrees and handles alternating runs. Use correctness-validation for accuracy claims.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
