# Command recipes

```bash
python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --model /data/models/Qwen --tp 2

python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py \
  --execution-id <id> --model /data/models/Qwen

python3 .agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py \
  --host 10.0.0.1 --model /data/models/Qwen \
  --state baseline=<commit> --state pr=pr:1
```
