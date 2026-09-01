# Benchmark Skill Behavior

## Relationship to remote-dev

Use `.remote-dev` tools for ad hoc remote read/edit/bash/search/patch around
benchmark setup and result inspection. This skill owns benchmark lifecycle and
keeps the existing scripts as the managed VAWS compatibility backend.

## Lifecycle

1. **Resolve target session** from `--session-id` / `--session-file`, or auto-resolve from the nearest `.vaws-local/current-session.json` worktree binding (cwd upward); fail fast if none is found.
2. **Assemble config** from user args + optional nightly reference.
3. **Stop existing service** in the target session if any; other sessions are never touched.
4. **Start service** via `serve_start.py` (which handles parity sync internally). If startup returns non-ready, call `serve_stop.py --force` for the same session before failing.
5. **Run benchmark iterations** via SSH on the remote container — all against the same warm service.
6. **Stop service** via `serve_stop.py` for the same session.
7. **Output structured JSON** on stdout and persist the result under `.vaws-local/sessions/<session-id>/benchmark/runs/`.

## Configuration Priority

User-provided arguments always take priority:

```
user CLI args  >  agent-assembled context  >  named preset (--preset)  >  nightly YAML fallback
```

Nightly YAML is a reference source for discovering how to configure a model/feature, not an execution template. When `--refer-nightly` is given:

- `server_cmd` and `server_cmd_extra` are merged (minus `--tensor-parallel-size` and `--port`, which are handled separately).
- `envs` are used as a base, with user `--extra-env` overriding.
- `benchmarks.perf` fields (`num_prompts`, `max_out_len`, `batch_size`) are mapped to bench CLI args.
- User-provided `--serve-args` / `--bench-args` completely override the nightly values.

A named preset (`presets/<name>.json`) slots between user args and nightly: it can
supply `tp`/`dp`/`port`/`devices`/`served_model_name`/`health_timeout`, service
`env` and bench-side `bench_env` objects, `serve_args`/`bench_args` arrays,
`vllm_ref`, `runs`/`warmup_runs`, a `fixed_request_dataset` object, and
`bench_request_counts`. Any explicit CLI arg overrides the preset value for that
field. Presets never carry a `model` path.

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

Comparing multiple code states (baseline, PR, modified) is handled by `bench_compare.py` in a single call:

1. Each `--state LABEL=REF` is checked out in the container for vllm-ascend (and vllm when `--vllm-ref`/preset `vllm_ref` is set). Alignment is source-only — it never rebuilds custom ops.
2. After alignment and any optional `git apply` from `--remote-patch-file`, the effective state's native-input digest (`csrc`/`cmake`/requirements) is compared against the first state's. A mismatch fails the run because stale compiled artifacts would silently contaminate the comparison. An unavailable digest also fails closed. `--allow-stale-native` explicitly downgrades either condition to a `warnings` entry plus `native_input_changed: true` or `native_input_unverified: true`.
3. Every state is served and benched with the identical assembled configuration — from explicit CLI args or a shared `--preset` — including optional request-count cases (`--bench-request-counts`), a generated fixed dataset (`--fixed-request-dataset`, prepared once before the state loop), a deterministic accuracy probe (`--accuracy-probe`), and an optional `git apply` of a local patch per state (`--remote-patch-file`).
4. Each completed state is persisted immediately; on failure the error JSON still carries `partial_states` and `result_paths` so completed measurements are never lost.

Only when `bench_compare.py` cannot express the setup does the agent fall back to
orchestrating `bench_run.py` once per state (switching local worktrees between
runs) and comparing the returned JSON itself.

### Comparison contract

For performance regression comparisons, all runs must use identical core benchmark parameters (`--serve-args`, `--bench-args`, `--extra-env`, `--tp`). Only the code state should change between runs. If any configuration parameter differs, the agent must explicitly record the difference in its output and treat the result as a **configuration comparison**, not a pure regression comparison.

### Regression判定

Given baseline throughput `T_b` and patched throughput `T_p`, compute the ratio `r = T_p / T_b`. If `r < 0.97`, the patched version is considered a throughput regression. The same threshold applies to `acceptance_rate` when speculative decoding is enabled. TTFT and TPOT regressions use inverted comparison (`r = T_b / T_p`) since lower is better for latency metrics.

## Remote Execution

`vllm bench serve` runs inside the session container via SSH. The result JSON file is written to `/tmp/` with a session token, local process id, and random suffix in the file name, then `cat`-ed back through the SSH session. The script parses the last JSON object from stdout. The unique file name matters because session containers can share the host `/tmp` mount on the same machine.

## Defaults

When the user provides no bench args and no nightly reference:
- `--num-prompts 64`
- `--max-concurrency 16`

These are conservative defaults suitable for a quick smoke test. For production benchmarking, users should specify explicit parameters.

## State Management

The structured JSON is returned on stdout for the agent or user to consume, and each run's result is also written under `.vaws-local/sessions/<session-id>/benchmark/runs/`. The serving skill handles its own state at `.vaws-local/sessions/<session-id>/serving.json` — the only serving state location.
