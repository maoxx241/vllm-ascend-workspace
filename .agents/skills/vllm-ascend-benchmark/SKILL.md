---
name: vllm-ascend-benchmark
description: Measure throughput and latency of a vLLM online service with a specified workload, including request-rate and concurrency sweeps. Use for 跑 benchmark, 压测, or 测吞吐. Code baseline-versus-candidate comparisons and performance regression decisions use vllm-ascend-performance-regression; accuracy and service lifecycle have separate workflows.
---

# vllm-ascend-benchmark

Measure a vLLM service with one or several benchmark iterations and return raw and normalized metrics.

Choose input/output lengths, concurrency, request rate and endpoint for the intended workload. User choices override presets and nightly examples. Report variance and failures alongside throughput and latency.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py --model /models/example --runs 3 --warmup-runs 1
```

Use --execution-id to measure an existing service, or let the workflow start and clean up its own service. --serve-args and --bench-args forward business options; --preset supplies reusable defaults. The managed interpreter and actual launch observations are recorded with measurements.

Use performance-regression for code comparisons: it binds actual local worktrees and handles alternating runs. Use correctness-validation for accuracy claims.

Read the relevant detail only when needed:
