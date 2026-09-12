# Command recipes

```bash
uv run --no-project python .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --model /data/models/Qwen --served-model-name Qwen --tp 2 \
  --tag smoke --mode enforce_eager --request-kind text \
  --benchmark-output-tokens 32

uv run --no-project python .agents/skills/ascend-profiling-collection/scripts/profile_control.py \
  --service vllm --action start_profile

uv run --no-project python .agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py \
  --execution-id <id> --profile-root /vllm-workspace/.vaws-runtime/profiling
```
