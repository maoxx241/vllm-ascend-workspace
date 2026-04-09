# Benchmark Skill Behavior

## Lifecycle

1. **Resolve machine** from local inventory (same as serving).
2. **Assemble config** from user args + optional nightly reference.
3. **Stop existing service** on the target machine if any.
4. **Start service** via `serve_start.py` (which handles parity sync internally).
5. **Run benchmark iterations** via SSH on the remote container — all against the same warm service.
6. **Stop service** via `serve_stop.py`.
7. **Output structured JSON** on stdout.

## Configuration Priority

User-provided arguments always take priority:

```
user CLI args  >  agent-assembled context  >  nightly YAML fallback
```

Nightly YAML is a reference source for discovering how to configure a model/feature, not an execution template. When `--refer-nightly` is given:

- `server_cmd` and `server_cmd_extra` are merged (minus `--tensor-parallel-size` and `--port`, which are handled separately).
- `envs` are used as a base, with user `--extra-env` overriding.
- `benchmarks.perf` fields (`num_prompts`, `max_out_len`, `batch_size`) are mapped to bench CLI args.
- User-provided `--serve-args` / `--bench-args` completely override the nightly values.

## Multi-Run (Warm-Service) Mode

When `--runs N` is given with N > 1, the service starts once and all N iterations run sequentially against the same warm service instance. The service is never restarted between runs.

`--warmup-runs M` excludes the first M runs from the aggregated statistics. This accounts for JIT compilation, graph capture, and other one-time costs that skew initial measurements.

### Why warm-service matters

Restarting the service between runs means every run pays the full startup cost (model loading, graph capture, JIT). The "discard first run" strategy only works when subsequent runs hit the already-warm service. If the service restarts each time, there are no warm runs to keep.

### Aggregation

The output JSON includes:

- `per_run`: every run's metrics, tagged with `warmup: true/false`
- `aggregated`: mean + sample stddev over the non-warmup runs, for each metric key
- `aggregated.count`: number of runs included in the statistics

## Multi-State Comparison

Comparing multiple code states (baseline, PR, modified) is **not** handled by a single benchmark script. Instead:

1. The agent switches the local workspace to each code state.
2. The agent calls `bench_run.py` once per state (each call does parity sync + service lifecycle + N runs).
3. The agent compares the returned JSON metrics across states.

This keeps the benchmark script focused on one thing (reliable measurement) and leaves orchestration to the agent, which is better equipped to handle git worktrees, cross-fork commits, and submodule complexity.

### Comparison contract

For performance regression comparisons, all runs must use identical core benchmark parameters (`--serve-args`, `--bench-args`, `--extra-env`, `--tp`). Only the code state should change between runs. If any configuration parameter differs, the agent must explicitly record the difference in its output and treat the result as a **configuration comparison**, not a pure regression comparison.

### Regression判定

Given baseline throughput `T_b` and patched throughput `T_p`, compute the ratio `r = T_p / T_b`. If `r < 0.97`, the patched version is considered a throughput regression. The same threshold applies to `acceptance_rate` when speculative decoding is enabled. TTFT and TPOT regressions use inverted comparison (`r = T_b / T_p`) since lower is better for latency metrics.

## Remote Execution

`vllm bench serve` runs inside the container via SSH. The result JSON file is written to `/tmp/` and `cat`-ed back through the SSH session. The script parses the last JSON object from stdout.

## Defaults

When the user provides no bench args and no nightly reference:
- `--num-prompts 64`
- `--max-concurrency 16`

These are conservative defaults suitable for a quick smoke test. For production benchmarking, users should specify explicit parameters.

## State Management

Benchmark results are not persisted locally by default. The structured JSON is returned on stdout for the agent or user to consume. The serving skill handles its own state under `.vaws-local/serving/`.
