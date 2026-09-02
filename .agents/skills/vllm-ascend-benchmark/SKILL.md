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
- **Multi-state comparisons** (baseline vs PR vs modified) are a first-class workflow: use `bench_compare.py`, which checks out each git ref *in the container*, benchmarks every state with identical serve/bench args, and reports TPOT/throughput deltas. Do not hand-write a bespoke comparison script — put reusable model/service configurations into a named preset under `presets/` instead (see below). The old bespoke `.agents/scripts/dsv4_flash_benchmark.py` was deleted; `presets/dsv4-flash.json` carries its DSV4 Flash configuration, with the two loader args adapted (`enable_multithread_load` as a JSON boolean; the old `--safetensors-load-strategy prefetch` was dropped in favor of multithreaded loading — the flag still exists at the pinned vllm ref 967c5c3b, so this is a deliberate replacement, not an upstream removal — verified on real A3 hardware). Note `bench_compare.py` runs back-to-back iterations with no inter-run sleep (the old script slept 15s between rounds), so absolute numbers are not directly comparable to historical bespoke-script results.
- **Native-input gate.** `bench_compare.py` aligns source only and never rebuilds compiled custom ops. After each state's checkout and optional `--remote-patch-file` application, it fingerprints the effective in-container `csrc`/`cmake`/requirements inputs and compares the digest against the first state's. A mismatch fails the run with an explanation. An unavailable digest also fails closed. Pass `--allow-stale-native` only to explicitly downgrade either condition to a loud warning plus `native_input_changed: true` or `native_input_unverified: true`.
- **Partial results are never lost.** Each completed state is persisted under the session's `benchmark/runs/` dir as it finishes; on any failure the error JSON still carries `partial_states` (completed labels) and `result_paths`.
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
  [--preset <name>] \
  [--tp <N>] [--dp <N>] \
  [--port <N>] [--devices <csv>] \
  [--served-model-name <name>] [--health-timeout <sec>] \
  [--runs <N>] \
  [--warmup-runs <M>] \
  [--serve-args <arg> ...] \
  [--bench-args <arg> ...] \
  [--extra-env KEY=VALUE ...] \
  [--bench-env KEY=VALUE ...] \
  [--refer-nightly <yaml-name>] \
  [--skip-parity]
```

- `--preset`: named preset JSON from the skill's `presets/` dir (e.g. `dsv4-flash`). Explicit CLI args override preset values.
- `--runs`: number of benchmark iterations against the same warm service (default: 1). The service starts once and all runs hit the same warm instance.
- `--warmup-runs`: number of initial runs to discard from aggregated statistics (default: 0). Must be less than `--runs`.
- `--serve-args`: extra arguments forwarded to `vllm serve` (e.g. `--async-scheduling`, `--compilation-config '...'`)
- `--bench-args`: extra arguments forwarded to `vllm bench serve` (e.g. `--num-prompts 128`, `--max-concurrency 32`)
- `--extra-env`: environment variables for the service (e.g. `HCCL_BUFFSIZE=1024`)
- `--bench-env`: environment variables exported in the bench-side remote shell before `vllm bench serve` (e.g. `PYTHONPATH=/vllm-workspace/vllm`)
- `--refer-nightly`: name of a nightly YAML (without path prefix) to use as a configuration reference; user-provided args override anything from the YAML

## Presets

A preset is a JSON file under `.agents/skills/vllm-ascend-benchmark/presets/<name>.json` holding a reusable, model-specific benchmark configuration so it never has to be re-typed (or hand-scripted) per comparison. Recognized keys:

- `tp`, `dp`, `port`, `devices`, `served_model_name`, `health_timeout` — service shape
- `env` (object) — service env vars, merged over nightly with CLI `--extra-env` winning
- `bench_env` (object) — bench-side remote-shell env vars (e.g. `PYTHONPATH`, `VLLM_VERSION`), CLI `--bench-env` wins
- `serve_args` / `bench_args` (arrays) — full arg lists, replaced wholesale by CLI `--serve-args` / `--bench-args`
- `vllm_ref` — vllm commit aligned in-container for every `bench_compare.py` state (CLI `--vllm-ref` wins)
- `runs`, `warmup_runs` — iteration defaults for `bench_compare.py` (CLI wins, fallback 3/1)
- `fixed_request_dataset` (`{input_len, output_len, path}`) — enables the generated fixed-token-count dataset in `bench_compare.py`
- `bench_request_counts` (array of int) — request-count cases for `bench_compare.py`

Per-field priority is always: **explicit CLI arg > preset > nightly YAML > built-in default**. A preset never carries a `model` key — the model weight path is machine-specific and `--model` stays required.

Shipped presets:

- `dsv4-flash` — DeepSeek-V4-Flash W4A8 MTP (tp8, port 30001, served name `dsv4-w4a8`, 6 runs / 1 warmup, fixed 512/512 config). This is the replacement for the deleted bespoke `.agents/scripts/dsv4_flash_benchmark.py`; do not re-create such one-off scripts — extend or add a preset instead.

## Workflow

### 1. Resolve the target session

The session comes from `--session-id` / `--session-file`, or is auto-resolved from the nearest `.vaws-local/current-session.json` worktree binding. If neither is given and no binding is found, the command fails fast and tells the user to pass `--session-id` or create a session with `session_create.py`.

### 2. Assemble configuration

Configuration is built with this priority:

1. User-provided CLI args (highest priority)
2. Agent-assembled args based on conversation context
3. Named preset from `--preset` (per field)
4. Nightly YAML as fallback when `--refer-nightly` is given and no user args override

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
    "spec_decode_acceptance_rate": 0.85
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
    "spec_decode_acceptance_rate": { "mean": 0.572, "stddev": 0.01, "values": [...] }
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
requires a rebuild, do that through parity/serve first. Parity sync is always
skipped inside `bench_compare.py` (it would overwrite the checked-out state);
there is no `--skip-parity` flag.

```bash
# Preset-driven (DSV4 Flash baseline vs PR, fixed dataset + accuracy probe)
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  --preset dsv4-flash \
  --model /home/weights/DeepSeek-V4-Flash-w4a8-mtp \
  --state baseline=<commit> --state pr10805=pr:10805 \
  --stale-cleanup --fixed-request-dataset --accuracy-probe

# Fully explicit
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  [--session-id <id> | --session-file <path>] \
  --model <remote-weight-path> \
  --state baseline=<commit> --state pr10741=pr:10741 \
  [--preset <name>] [--vllm-ref <vllm-commit>] \
  [--tp <N>] [--dp <N>] [--port <N>] [--devices <csv>] \
  [--served-model-name <name>] [--health-timeout <sec>] \
  [--runs <N>] [--warmup-runs <M>] \
  [--bench-request-counts 1,2] \
  [--fixed-request-dataset [--fixed-input-len N] [--fixed-output-len N] \
   [--fixed-dataset-path <path>] [--fixed-prompt <text>]] \
  [--accuracy-probe [--accuracy-prompt <text>] [--accuracy-max-tokens N]] \
  [--remote-patch-file <local.patch>] [--allow-stale-native] \
  [--bench-env KEY=VALUE ...] [--stale-cleanup] \
  [--serve-args <arg> ...] \
  [--bench-args <arg> ...]
```

Extra flags beyond `bench_run.py`:

- `--bench-request-counts 1,2`: run one case per count on the same warm service; each count overrides both `--num-prompts` and `--max-concurrency` for that case. Cases are labeled `requests_<N>` (the unset case is `default`) and compared per case against the first state's same case.
- `--fixed-request-dataset`: generate a JSONL dataset of identical fixed-token-count requests on the remote once before the state loop (it depends only on model/tokenizer), then bench it via `--dataset-name custom ... --skip-chat-template --disable-shuffle --ignore-eos`. Auto-enabled when the preset carries `fixed_request_dataset`; CLI `--fixed-*` flags override preset values. The result (incl. `prompt_sha256`) is recorded under `fixed_dataset` in the final JSON.
- `--accuracy-probe`: after the service is ready and before benching, POST one temperature-0 completion and record `text_sha256`/`finish_reason`/`usage` per state, so silent numeric regressions surface alongside performance deltas.
- `--remote-patch-file`: `git apply` a local patch to the in-container vllm-ascend checkout after each state's alignment (e.g. validation instrumentation), recorded per state.
- `--allow-stale-native`: downgrade the native-input gate failure to a warning (see Critical rules).

Output is a single JSON object with `comparison` (per-state, per-case mean TPOT,
throughput, spec-decode acceptance rate, and `delta_tpot_pct_vs_first`) plus full
`state_results`, the effective `config` summary (so preset-driven serve/bench
args stay traceable), `preset`, `warnings`, `result_paths`, and `fixed_dataset`.
`--stale-cleanup` runs the SAFE cleanup before/after each state. Every
completed state is persisted under the session's `benchmark/runs/` dir as it
finishes; a failure still prints `partial_states` and `result_paths`.

For a single state, keep using `bench_run.py`.

## Reference files

- `.agents/skills/vllm-ascend-benchmark/references/behavior.md`
- `.agents/skills/vllm-ascend-benchmark/references/command-recipes.md`
- `.agents/skills/vllm-ascend-benchmark/references/acceptance.md`
