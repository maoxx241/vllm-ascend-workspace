---
name: vllm-ascend-benchmark
description: Run vLLM online-serving benchmarks on a workspace-managed remote container. Use for requests like "跑个 benchmark", "对比性能", "压测一下", "测下吞吐", or "看下有没有性能回退". Do not use for accuracy tests, nightly CI matrix runs, offline inference, or service-only lifecycle.
---

# vLLM Ascend Benchmark

Run `vllm bench serve` on a **ready** session-managed remote container and produce structured performance results. Supports single-run and multi-run (warm-service) modes.

Remote substrate rule: use `.remote-dev` remote tools for ad hoc remote
read/edit/bash/search/patch work around benchmark setup or result inspection.
Use this skill for the domain benchmark workflow and keep its scripts as the
compatibility backend for managed VAWS sessions.

## Use this skill when

- the user asks to run a performance benchmark / throughput test in a managed session
- the user asks to compare performance before and after a code change
- the user asks to verify there is no performance regression for a PR or commit

## Do not use this skill when

- the task is accuracy testing (aisbench domain)
- the task is running a full nightly CI matrix
- the task is offline / batch inference
- the user only wants to start or stop a service without benchmarking (use `vllm-ascend-serving`)
- no session exists yet for the target (use `session-management` first)

## Critical rules

- Benchmark parameters are assembled by the agent based on user intent and executed through the scripts below. The agent must not construct raw `vllm bench serve` commands and run them directly on the remote.
- **User intent takes priority** over nightly configs. Nightly YAML files under `vllm-ascend/tests/e2e/nightly/single_node/models/configs/` are a **reference source** for discovering how to configure a given model or feature (MTP, graph mode, TP count, etc.), not an execution template to run verbatim.
- Nightly configs are used as a **fallback** only when the user specifies a model but provides no other parameters.
- Benchmarking is **session-only**. `bench_run.py` takes an optional `--session-id <id>` / `--session-file <path>`; when both are omitted, the session is auto-resolved from the nearest `.vaws-local/current-session.json` worktree binding (cwd upward), so running from inside a session worktree needs zero target arguments. If no binding is found, the command fails fast with instructions to pass `--session-id` or create a session with `session-management`'s `session_create.py`.
- After benchmarking, the service is automatically stopped. No residual processes should remain. Cleanup stops only that session's service.
- If service startup returns a non-ready result after launching a PID, benchmark cleanup still calls `serve_stop.py --force` for the same session.
- Progress goes to `stderr` as `__VAWS_BENCHMARK_PROGRESS__=<json>`. Final result goes to `stdout` as JSON.
- Keep local benchmark state under `.vaws-local/sessions/<session-id>/benchmark/`; results are written to `.vaws-local/sessions/<session-id>/benchmark/runs/`.
- **Multi-state comparisons** (baseline vs PR vs modified) are a first-class workflow: use `bench_compare.py`, which checks out each git ref *in the container*, benchmarks every state with identical serve/bench args, and reports TPOT/throughput deltas. Do not hand-write a bespoke comparison script.
- **Never hand-roll stale-process cleanup.** A past bespoke cleanup SIGTERM'd a session's dedicated sshd (`Exiting on signal 15`), dropped the container SSH port, and forced a rebuild. Use `--stale-cleanup` (backed by `safe_stale_cleanup`), which only reaps vLLM `EngineCore`/`Worker` children by name, skips PID 1, excludes anything matching `sshd`/`vaws`, and kills explicit pids only (never a process group).
- **Backend is overridable.** The default is chat (`--backend openai-chat --endpoint /v1/chat/completions`). For completion-style models (e.g. DSV4) pass `--bench-args --backend openai --endpoint /v1/completions ...` and the default is not injected.

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

## Public entry point

```bash
# Inside a session worktree the session is auto-resolved — no target flag needed.
# Outside a worktree, pass --session-id <id> (or --session-file <path>).
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  [--session-id <id> | --session-file <path>] \
  --model <remote-weight-path> \
  [--tp <N>] [--dp <N>] \
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

### 1. Resolve the target session

The session comes from `--session-id` / `--session-file`, or is auto-resolved from the nearest `.vaws-local/current-session.json` worktree binding. If neither is given and no binding is found, the command fails fast and tells the user to pass `--session-id` or create a session with `session_create.py`.

### 2. Assemble configuration

Configuration is built with this priority:

1. User-provided CLI args (highest priority)
2. Agent-assembled args based on conversation context
3. Nightly YAML as fallback when `--refer-nightly` is given and no user args override

When `--refer-nightly` is used, the YAML is parsed for `server_cmd`, `envs`, and `benchmarks.perf` fields. Any user-provided `--serve-args`, `--bench-args`, or `--extra-env` override the corresponding YAML values.

### 3. Stop any existing service

If a service is already running in the target session, stop it before proceeding.

### 4. Start the service

Uses `serve_start.py` internally to launch the vLLM service with the assembled configuration. Parity sync is handled automatically by the serving skill.

If startup fails or times out after a remote PID was recorded, `bench_run.py` calls `serve_stop.py --force` before returning failure.

### 5. Run benchmark iterations

Executes `vllm bench serve` via SSH on the remote container against the running service. In multi-run mode, all iterations hit the same warm service instance — the service is **not** restarted between runs.

### 6. Stop the service

Calls `serve_stop.py` to clean up after all runs complete. If both the graceful
and the forced stop fail, the result keeps the collected metrics but reports
`"status": "cleanup_failed"` (plus a `cleanup_warning` field) and the script
exits non-zero, because the service is still holding NPU memory.

### 7. Return structured JSON

Single-run output (`--runs 1`, the default):

```json
{
  "status": "ok",
  "session_id": "pr123",
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
  "session_id": "pr123",
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

## Multi-state comparison (bench_compare.py)

Compare performance across git states in one call. Each `--state LABEL=REF` is
checked out **in the container** for vllm-ascend (and optionally vllm via
`--vllm-ref`), then benchmarked with the shared serve/bench args so any delta
is attributable to code. `REF` accepts `pr:NNNN` / `#NNNN` (GitHub PR head), a
commit SHA, or a branch. Alignment is source-only — it never rebuilds custom
ops (matching the common "对齐版本但不重编算子" workflow); if a kernel change
requires a rebuild, do that through parity/serve first.

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  [--session-id <id> | --session-file <path>] \
  --model <remote-weight-path> \
  --state baseline=<commit> --state pr10741=pr:10741 \
  [--vllm-ref <vllm-commit>] \
  [--tp <N>] [--dp <N>] [--runs <N>] [--warmup-runs <M>] \
  [--stale-cleanup] \
  [--serve-args <arg> ...] \
  [--bench-args <arg> ...]
```

Output is a single JSON object with `comparison` (per-state mean TPOT,
throughput, acceptance rate, and `delta_tpot_pct_vs_first`) plus full
`state_results`. `--stale-cleanup` runs the SAFE cleanup before/after each
state. The result is persisted under the session's `benchmark/runs/` dir.

For a single state, keep using `bench_run.py`.

## Reference files

- `.agents/skills/vllm-ascend-benchmark/references/behavior.md`
- `.agents/skills/vllm-ascend-benchmark/references/command-recipes.md`
- `.agents/skills/vllm-ascend-benchmark/references/acceptance.md`
