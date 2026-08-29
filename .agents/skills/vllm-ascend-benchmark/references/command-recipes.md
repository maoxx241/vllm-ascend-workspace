# Benchmark Command Recipes

All benchmark commands are session-scoped. When run from inside a session worktree
(a directory with `.vaws-local/current-session.json`), the session is auto-resolved
and **no target flag is needed** — that is the primary form shown below. Outside a
worktree, add `--session-id <id>` (or `--session-file <path>`) explicitly.

## Single-run: minimal

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --model /home/weights/Qwen3.5-0.8B \
  --tp 1
```

Explicit session target:

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --session-id pr123 \
  --model /home/weights/Qwen3.5-0.8B \
  --tp 1
```

## Single-run: full-featured (MTP + graph mode)

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --model /home/weights/Qwen3-Next-80B-A3B-Instruct \
  --tp 4 \
  --extra-env OMP_NUM_THREADS=10 \
  --extra-env HCCL_BUFFSIZE=1024 \
  --extra-env PYTORCH_NPU_ALLOC_CONF=expandable_segments:True \
  --serve-args \
    --max-model-len 40960 \
    --trust-remote-code \
    --async-scheduling \
    --no-enable-prefix-caching \
    --enable-expert-parallel \
    --gpu-memory-utilization 0.8 \
    --max-num-seqs 64 \
    --compilation_config '{"cudagraph_mode": "FULL_DECODE_ONLY"}' \
    --speculative_config '{"method": "qwen3_5_mtp", "num_speculative_tokens": 3, "enforce_eager": true}' \
  --bench-args \
    --num-prompts 256 \
    --max-concurrency 64 \
    --output-len 1500
```

## Multi-run with warmup: statistical benchmarking

Start the service once, run 5 iterations, discard the first as warmup, aggregate the remaining 4:

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --model /home/weights/Qwen3.5-35B-A3B \
  --tp 4 \
  --runs 5 --warmup-runs 1 \
  --serve-args \
    --max-model-len 4096 \
    --trust-remote-code \
    --async-scheduling \
    --compilation_config '{"cudagraph_mode": "FULL_DECODE_ONLY", "cudagraph_capture_sizes": [4,8,12,16]}' \
    --speculative_config '{"method": "qwen3_5_mtp", "num_speculative_tokens": 3}' \
  --bench-args \
    --num-prompts 64 \
    --max-concurrency 16 \
    --output-len 1500
```

## Single-run: with nightly reference as fallback

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --model /home/weights/Qwen3-Next-80B-A3B-Instruct \
  --refer-nightly Qwen3-Next-80B-A3B-Instruct-A2
```

## Preset-driven runs

Named presets under `.agents/skills/vllm-ascend-benchmark/presets/` pin a reusable
model/service configuration (tp/dp/port/devices/served name/health timeout, env,
bench env, serve/bench args, vllm ref, runs/warmup, fixed dataset, request
counts). Explicit CLI args override preset values per field; `--model` is always
required because weight paths are machine-specific.

`dsv4-flash` carries the DeepSeek-V4-Flash W4A8 MTP configuration and replaces
the deleted bespoke `.agents/scripts/dsv4_flash_benchmark.py` — do not hand-write
new one-off benchmark scripts; add or extend a preset instead.

```bash
# Single-run with the DSV4 Flash preset
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --model /home/weights/DeepSeek-V4-Flash-w4a8-mtp \
  --preset dsv4-flash \
  --runs 6 --warmup-runs 1

# Multi-state comparison with the same preset (baseline vs PR), fixed dataset,
# deterministic accuracy probe, and safe stale cleanup
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  --preset dsv4-flash \
  --model /home/weights/DeepSeek-V4-Flash-w4a8-mtp \
  --state baseline=<commit> --state pr10805=pr:10805 \
  --stale-cleanup --fixed-request-dataset --accuracy-probe

# Multiple request-count cases in one comparison (each count overrides
# --num-prompts and --max-concurrency for its case)
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  --preset dsv4-flash \
  --model /home/weights/DeepSeek-V4-Flash-w4a8-mtp \
  --state baseline=<commit> --state pr10805=pr:10805 \
  --bench-request-counts 1,2 --fixed-request-dataset
```

## Multi-state comparison: agent-orchestrated (fallback)

The preferred path is a single `bench_compare.py` call (see the preset examples
above) — it aligns each git ref in-container, gates on native-input digests,
and persists every completed state. Only when `bench_compare.py` cannot express
the setup (e.g. each state needs a *different* local worktree synced through
parity) does the agent run `bench_run.py` once per state, switching the local
workspace between each. **Prefer git worktrees over checkout** — worktrees are
safer, support parallel runs, and avoid polluting the main working tree.

All runs must use identical `--serve-args`, `--bench-args`, `--extra-env`, and `--tp`.
Only the code state should differ (see comparison contract in `behavior.md`).

### Preferred: worktree-based

```bash
# Create isolated worktrees for each state
git -C vllm-ascend worktree add /tmp/bench-baseline main
git -C vllm-ascend worktree add /tmp/bench-pr feat/optimize

# State A: point vllm-ascend at baseline worktree, run benchmark
# (agent handles symlinking or parity sync with the worktree path)
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --session-id pr123 \
  --model /home/weights/Qwen3.5-35B-A3B \
  --tp 4 --runs 5 --warmup-runs 1 \
  --serve-args --async-scheduling \
  --bench-args --num-prompts 64 --max-concurrency 16

# State B: switch to PR worktree, run same benchmark
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --session-id pr123 \
  --model /home/weights/Qwen3.5-35B-A3B \
  --tp 4 --runs 5 --warmup-runs 1 \
  --serve-args --async-scheduling \
  --bench-args --num-prompts 64 --max-concurrency 16

# Cleanup
git -C vllm-ascend worktree remove /tmp/bench-baseline
git -C vllm-ascend worktree remove /tmp/bench-pr
```

### Fallback: checkout-based

When worktrees are impractical (e.g. cross-fork commits not yet fetched):

```bash
cd vllm-ascend && git checkout main && cd ..
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --model /home/weights/Qwen3.5-35B-A3B \
  --tp 4 --runs 5 --warmup-runs 1 \
  --serve-args --async-scheduling \
  --bench-args --num-prompts 64 --max-concurrency 16

cd vllm-ascend && git checkout feat/optimize && cd ..
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --model /home/weights/Qwen3.5-35B-A3B \
  --tp 4 --runs 5 --warmup-runs 1 \
  --serve-args --async-scheduling \
  --bench-args --num-prompts 64 --max-concurrency 16
```

The agent collects all JSON outputs and compares `aggregated.output_throughput.mean`,
`aggregated.mean_ttft_ms.mean`, `aggregated.acceptance_rate.mean`, etc. Each run's
result JSON is also persisted under `.vaws-local/sessions/<session-id>/benchmark/runs/`.
