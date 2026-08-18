# Prefill search and validation

## Search objective

Optimize QPS subject to the requested TTFT constraint and correctness requirements. For fixed-length Prefill with output=1, QPS and input-token throughput should move consistently.

Use the exact constrained metric. A run with average TTFT below the limit but P90 above a P90 SLO is infeasible.

## Staged search

### A. Baseline concurrency sweep

Start at the user’s requested concurrency. Increase in bounded steps appropriate to the topology, for example `16, 20, 24, 32`; avoid assuming powers of two are optimal.

For each point:

- Set `data_num = concurrency * 4`.
- Keep the service configuration fixed.
- Reset/warm prefix cache according to the case.
- Record actual concurrency, per-DP running requests when metrics expose it, and input length after the chat template.

Continue until at least one of these conditions applies:

- Two successive higher-concurrency points violate the TTFT SLO and throughput no longer improves.
- Capacity, allocation, or scheduler constraints make higher points invalid.
- The user’s explicit concurrency ceiling is reached.

An invalid/OOM point is evidence about capacity, not a latency result.

### B. Scheduler tuning

Tune one family at a time near the best feasible point:

- `max-num-batched-tokens`: controls total scheduled tokens. Ensure it supports the intended per-DP chunk concurrency without consuming unsafe activation memory.
- `long-prefill-token-threshold`: controls chunk size for long prompts. Common candidates are powers or implementation-supported values around the current setting; do not assume 2048 is universally optimal.
- `max-num-seqs`: must not be below the required per-engine concurrency and may affect warmup/capture memory.
- PP partition/topology: treat as a separate deployment configuration because it changes memory, communication, and layer balance.
- Compilation, fused communication, EPLB, or speculative decoding features: change one feature set at a time and retain exact environment variables/additional config.

Run a coarse one-factor sweep, then a small cross-product of only the best values. Avoid exhaustive grids that spend hours on dominated configurations.

### C. Confirmation

For the winning candidate:

1. Restart from its saved service script.
2. Clear `/root/ascend/log` before launch.
3. Wait for all workers and graph/warmup stages to finish.
4. Reset prefix cache; perform required prefix warmup.
5. Run a fresh `concurrency * 4` dataset.
6. Archive everything before another restart.

Prefer a second confirmation when results are noisy or the winner improves the runner-up by less than 3%.

## Prefix-cache validation

From before/after Prometheus snapshots, sum all relevant engines:

```text
queries_delta = queries_after - queries_before
hits_delta = hits_after - hits_before
hit_rate = hits_delta / queries_delta
```

Use `0` only when `queries_delta > 0` and `hits_delta == 0`. Mark unavailable if metrics are missing. A reasonable default tolerance is ±2 percentage points; use a stricter user-provided tolerance when present.

## Validity checks

Reject a run if any of these is true:

- `Failed Requests > 0`.
- Successful response has average output tokens below 1.
- Total valid responses differ from the intended data count.
- Console reports fast ~1s responses for a long case but output tokens are 0.
- Service log contains worker exception, EngineCore fatal error, OOM, HCCL timeout, RMSNorm/shape failure, or process termination during the run.
- Prefix hit rate is missing or outside tolerance when cache behavior is part of the case.
- TTFT metric is missing, non-finite, or violates the SLO.

AISBench can exit 0 after summarizing HTTP failures. Treat return code as tool execution status, not inference correctness.

## Metrics to retain

- TTFT average/min/max/median/P75/P90/P99.
- QPS/request throughput and benchmark duration.
- Actual input/output tokens and input/output/total throughput.
- Intended and actual concurrency.
- Prefix query/hit deltas and hit ratio per engine and aggregate.
- Peak KV-cache utilization and running/waiting request counts.
- Peak device memory from `npu-smi` sampling when available.
- Service startup time, graph capture memory/time, KV-cache capacity, and fatal-warning summary.

## Performance comparison

For a lower-is-better metric:

```text
improvement_pct = (baseline - candidate) / baseline * 100
```

For a higher-is-better metric:

```text
improvement_pct = (candidate - baseline) / baseline * 100
```

Never compare an invalid run or error-response TTFT against a successful baseline.
