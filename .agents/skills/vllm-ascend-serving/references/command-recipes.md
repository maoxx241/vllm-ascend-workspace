# Command Recipes

```bash
python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --model /data/models/Qwen3-32B \
  --tp 4 \
  --service vllm

python3 .agents/skills/vllm-ascend-serving/scripts/serve_start.py \
  --relaunch \
  --service vllm

python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py --service vllm
python3 .agents/skills/vllm-ascend-serving/scripts/serve_status.py --execution-id <id>
python3 .agents/skills/vllm-ascend-serving/scripts/serve_stop.py --service vllm
python3 .agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py --host 10.0.0.1
```

`--relaunch` submits `restart=True`. Queued starts keep the same execution
id; do not resubmit. Managed sources are prepared by coordinator; do not
call `parity_sync.py --execution-id` against a live root.
