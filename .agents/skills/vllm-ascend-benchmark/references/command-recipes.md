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

## Single-run: with nightly reference as fallback

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --machine 173.131.1.2 \
  --model /home/weights/Qwen3-Next-80B-A3B-Instruct \
  --refer-nightly Qwen3-Next-80B-A3B-Instruct-A2
```

## A/B comparison: same code, different env

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  --machine 173.125.1.2 \
  --baseline-ref main \
  --patched-ref main \
  --repo vllm-ascend \
  --model /home/weights/Qwen3.5-0.8B \
  --tp 1 \
  --patched-extra-env VLLM_LOGGING_LEVEL=DEBUG
```

## A/B comparison: PR vs main

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  --machine 173.131.1.2 \
  --baseline-ref main \
  --patched-ref feat/optimize-decode \
  --repo vllm-ascend \
  --model /home/weights/Qwen3-Next-80B-A3B-Instruct \
  --tp 4 \
  --extra-env HCCL_BUFFSIZE=1024 \
  --serve-args \
    --max-model-len 40960 \
    --async-scheduling \
    --compilation_config '{"cudagraph_mode": "FULL_DECODE_ONLY"}'
```

## A/B comparison: patched branch needs a feature flag

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  --machine 173.125.1.2 \
  --baseline-ref main \
  --patched-ref feat/new-scheduler \
  --repo vllm-ascend \
  --model /home/weights/Qwen3.5-0.8B \
  --tp 1 \
  --patched-extra-env VLLM_ENABLE_NEW_SCHEDULER=1
```
