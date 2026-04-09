# Benchmark Command Recipes

## Single-run: minimal

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --machine 173.131.1.2 \
  --model /home/weights/Qwen3.5-0.8B \
  --tp 1
```

## Single-run: full-featured (MTP + graph mode)

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --machine 173.131.1.2 \
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
  --machine 173.131.1.2 \
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
  --machine 173.131.1.2 \
  --model /home/weights/Qwen3-Next-80B-A3B-Instruct \
  --refer-nightly Qwen3-Next-80B-A3B-Instruct-A2
```

## Multi-state comparison: agent-orchestrated

To compare multiple code states (baseline / PR / modified), the agent runs `bench_run.py`
once per state, switching the local workspace between each. **Prefer git worktrees over
checkout** — worktrees are safer, support parallel runs, and avoid polluting the main
working tree.

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
  --machine 173.131.1.2 \
  --model /home/weights/Qwen3.5-35B-A3B \
  --tp 4 --runs 5 --warmup-runs 1 \
  --serve-args --async-scheduling \
  --bench-args --num-prompts 64 --max-concurrency 16

# State B: switch to PR worktree, run same benchmark
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --machine 173.131.1.2 \
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
  --machine 173.131.1.2 \
  --model /home/weights/Qwen3.5-35B-A3B \
  --tp 4 --runs 5 --warmup-runs 1 \
  --serve-args --async-scheduling \
  --bench-args --num-prompts 64 --max-concurrency 16

cd vllm-ascend && git checkout feat/optimize && cd ..
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --machine 173.131.1.2 \
  --model /home/weights/Qwen3.5-35B-A3B \
  --tp 4 --runs 5 --warmup-runs 1 \
  --serve-args --async-scheduling \
  --bench-args --num-prompts 64 --max-concurrency 16
```

The agent collects all JSON outputs and compares `aggregated.output_throughput.mean`,
`aggregated.mean_ttft_ms.mean`, `aggregated.acceptance_rate.mean`, etc.
