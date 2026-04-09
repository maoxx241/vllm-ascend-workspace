---
name: vllm-ascend-benchmark
description: Run vLLM online-serving benchmarks on a workspace-managed remote container. Use for requests like "跑个 benchmark", "对比性能", "压测一下", "测下吞吐", or "看下有没有性能回退". Do not use for accuracy tests, nightly CI matrix runs, offline inference, or service-only lifecycle.
---

# vLLM Ascend Benchmark

Run `vllm bench serve` on a **ready** workspace-managed remote container and produce structured performance results. Supports single-run and multi-run (warm-service) modes.

## Use this skill when

- the user asks to run a performance benchmark / throughput test on a managed machine
- the user asks to compare performance before and after a code change
- the user asks to verify there is no performance regression for a PR or commit

## Do not use this skill when

- the task is accuracy testing (aisbench domain)
- the task is running a full nightly CI matrix
- the task is offline / batch inference
- the user only wants to start or stop a service without benchmarking (use `vllm-ascend-serving`)
- the machine is not yet ready in inventory (use `machine-management` first)

## Critical rules

- Benchmark parameters are assembled by the agent based on user intent and executed through the scripts below. The agent must not construct raw `vllm bench serve` commands and run them directly on the remote.
- **User intent takes priority** over nightly configs. Nightly YAML files under `vllm-ascend/tests/e2e/nightly/single_node/models/configs/` are a **reference source** for discovering how to configure a given model or feature (MTP, graph mode, TP count, etc.), not an execution template to run verbatim.
- Nightly configs are used as a **fallback** only when the user specifies a model but provides no other parameters.
- After benchmarking, the service is automatically stopped. No residual processes should remain.
- Progress goes to `stderr` as `__VAWS_BENCHMARK_PROGRESS__=<json>`. Final result goes to `stdout` as JSON.
- Keep local benchmark state only under `.vaws-local/benchmark/`.
- **Multi-state comparisons** (e.g. baseline vs PR vs modified) are orchestrated by the agent calling `bench_run.py` once per code state, not by a single script. The agent is responsible for switching code states (via worktree, checkout, or manual edit) and running parity between each state.

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

## Public entry point

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --machine <alias-or-ip> \
  --model <remote-weight-path> \
  [--tp <N>] \
  [--runs <N>] \
  [--warmup-runs <M>] \
  [--serve-args <arg> ...] \
  [--bench-args <arg> ...] \
  [--extra-env KEY=VALUE ...] \
  [--refer-nightly <yaml-name>] \
  [--port <N>] \
  [--skip-parity]
```

- `--runs`: number of benchmark iterations against the same warm service (default: 1). The service starts once and all runs hit the same warm instance.
- `--warmup-runs`: number of initial runs to discard from aggregated statistics (default: 0). Must be less than `--runs`.
- `--serve-args`: extra arguments forwarded to `vllm serve` (e.g. `--async-scheduling`, `--compilation-config '...'`)
- `--bench-args`: extra arguments forwarded to `vllm bench serve` (e.g. `--num-prompts 128`, `--max-concurrency 32`)
- `--extra-env`: environment variables for the service (e.g. `HCCL_BUFFSIZE=1024`)
- `--refer-nightly`: name of a nightly YAML (without path prefix) to use as a configuration reference; user-provided args override anything from the YAML

## Workflow

### 1. Resolve the target machine

The `--machine` argument is looked up in the local machine inventory. The machine must already be managed and ready.

### 2. Assemble configuration

Configuration is built with this priority:

1. User-provided CLI args (highest priority)
2. Agent-assembled args based on conversation context
3. Nightly YAML as fallback when `--refer-nightly` is given and no user args override

When `--refer-nightly` is used, the YAML is parsed for `server_cmd`, `envs`, and `benchmarks.perf` fields. Any user-provided `--serve-args`, `--bench-args`, or `--extra-env` override the corresponding YAML values.

### 3. Stop any existing service

If a service is already running on the target machine, stop it before proceeding.

### 4. Start the service

Uses `serve_start.py` internally to launch the vLLM service with the assembled configuration. Parity sync is handled automatically by the serving skill.

### 5. Run benchmark iterations

Executes `vllm bench serve` via SSH on the remote container against the running service. In multi-run mode, all iterations hit the same warm service instance — the service is **not** restarted between runs.

### 6. Stop the service

Calls `serve_stop.py` to clean up after all runs complete.

### 7. Return structured JSON

Single-run output (`--runs 1`, the default):

```json
{
  "status": "ok",
  "machine": "173.131.1.2",
  "model": "/home/weights/Qwen3.5-35B",
  "metrics": {
    "output_throughput": 1234.5,
    "mean_tpot_ms": 12.3,
    "mean_ttft_ms": 45.6,
    "acceptance_rate": 0.85
  },
  "config": { "tp": 4, "serve_args": [...], "bench_args": [...], "env": {...} }
}
```

Multi-run output (`--runs N` where N > 1):

```json
{
  "status": "ok",
  "machine": "173.131.1.2",
  "model": "/home/weights/Qwen3.5-35B",
  "runs": 5,
  "warmup_runs": 1,
  "aggregated": {
    "count": 4,
    "output_throughput": { "mean": 165.2, "stddev": 2.1, "values": [163.5, 165.1, 166.8, 165.4] },
    "mean_ttft_ms": { "mean": 1020.5, "stddev": 15.3, "values": [...] },
    "acceptance_rate": { "mean": 0.572, "stddev": 0.01, "values": [...] }
  },
  "per_run": [
    { "run": 1, "warmup": true, "metrics": {...} },
    { "run": 2, "warmup": false, "metrics": {...} },
    ...
  ],
  "config": { "tp": 4, "serve_args": [...], "bench_args": [...], "env": {...} }
}
```

## Multi-state comparison pattern

To compare performance across code states (e.g. baseline vs PR), the agent should:

1. For each code state, ensure the local workspace reflects that state (checkout, worktree, or revert).
2. Call `bench_run.py` with `--runs N --warmup-runs M` for that state.
3. Collect the JSON output for each state.
4. Compare the `aggregated` metrics across states.

The agent orchestrates the code-state switching and parity syncing between runs. This is more flexible and robust than a single script trying to manage git operations, because the agent can handle edge cases like cross-fork commits and submodule quirks.

## Reference files

- `.agents/skills/vllm-ascend-benchmark/references/behavior.md`
- `.agents/skills/vllm-ascend-benchmark/references/command-recipes.md`
- `.agents/skills/vllm-ascend-benchmark/references/acceptance.md`
