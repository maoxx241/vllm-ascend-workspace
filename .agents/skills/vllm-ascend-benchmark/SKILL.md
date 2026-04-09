---
name: vllm-ascend-benchmark
description: Run vLLM online-serving benchmarks on a workspace-managed remote container. Use for requests like "跑个 benchmark", "对比性能", "压测一下", "测下吞吐", or "看下有没有性能回退". Do not use for accuracy tests, nightly CI matrix runs, offline inference, or service-only lifecycle.
---

# vLLM Ascend Benchmark

Run `vllm bench serve` on a **ready** workspace-managed remote container and produce structured performance results. Supports single-run benchmarks and A/B comparisons.

## Use this skill when

- the user asks to run a performance benchmark / throughput test on a managed machine
- the user asks to compare performance before and after a code change (A/B)
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
- A/B comparisons must use identical core benchmark parameters (model, tp, bench-args) for both sides — only the code state changes. However, the patched side may carry additional environment variables (debug switches, feature flags) via `--patched-extra-env`, and the env difference must be recorded in the output.
- After benchmarking, the service is automatically stopped. No residual processes should remain.
- Progress goes to `stderr` as `__VAWS_BENCHMARK_PROGRESS__=<json>`. Final result goes to `stdout` as JSON.
- Keep local benchmark state only under `.vaws-local/benchmark/`.

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

## Public entry points

### Single-run benchmark

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --machine <alias-or-ip> \
  --model <remote-weight-path> \
  [--tp <N>] \
  [--serve-args <arg> ...] \
  [--bench-args <arg> ...] \
  [--extra-env KEY=VALUE ...] \
  [--refer-nightly <yaml-name>] \
  [--port <N>] \
  [--skip-parity]
```

- `--serve-args`: extra arguments forwarded to `vllm serve` (e.g. `--async-scheduling`, `--compilation-config '...'`)
- `--bench-args`: extra arguments forwarded to `vllm bench serve` (e.g. `--num-prompts 128`, `--max-concurrency 32`)
- `--extra-env`: environment variables for the service (e.g. `HCCL_BUFFSIZE=1024`)
- `--refer-nightly`: name of a nightly YAML (without path prefix) to use as a configuration reference; user-provided args override anything from the YAML

### A/B comparison

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  --machine <alias-or-ip> \
  --baseline-ref <branch-or-commit> \
  --patched-ref <branch-or-commit> \
  --repo <vllm-ascend|vllm> \
  --model <remote-weight-path> \
  [--patched-extra-env KEY=VALUE ...] \
  [--tp <N>] \
  [--serve-args <arg> ...] \
  [--bench-args <arg> ...] \
  [--extra-env KEY=VALUE ...] \
  [--refer-nightly <yaml-name>] \
  [--port <N>]
```

- `--patched-extra-env`: environment variables applied **only** to the patched side (e.g. a debug flag the patched branch introduces). The env difference is recorded in the output.
- Core benchmark parameters are shared between both runs.

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

### 5. Run `vllm bench serve`

Executes `vllm bench serve` via SSH on the remote container against the running service endpoint.

### 6. Collect results

Parses the benchmark output JSON for key metrics: `output_throughput`, `mean_tpot_ms`, `mean_ttft_ms`, and (when applicable) `acceptance_rate`.

### 7. Stop the service

Calls `serve_stop.py` to clean up.

### 8. Return structured JSON

Single-run output:

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

A/B comparison output:

```json
{
  "status": "ok",
  "machine": "173.131.1.2",
  "model": "/home/weights/Qwen3.5-35B",
  "baseline": { "ref": "main", "metrics": {...}, "env": {...} },
  "patched":  { "ref": "feat/xxx", "metrics": {...}, "env": {...} },
  "delta": {
    "output_throughput": "+3.2%",
    "mean_tpot_ms": "-1.5%"
  },
  "env_diff": { "patched_only": ["VLLM_XXX=1"] },
  "regression": false
}
```

## Reference files

- `.agents/skills/vllm-ascend-benchmark/references/behavior.md`
- `.agents/skills/vllm-ascend-benchmark/references/command-recipes.md`
- `.agents/skills/vllm-ascend-benchmark/references/acceptance.md`
