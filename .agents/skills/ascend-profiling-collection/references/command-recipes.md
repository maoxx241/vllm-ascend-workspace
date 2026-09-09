# Command recipes

```bash
python3 .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --model /data/models/Qwen --served-model-name Qwen --tp 2 \
  --tag smoke --mode enforce_eager --request-kind text \
  --benchmark-output-tokens 32

python3 .agents/skills/ascend-profiling-collection/scripts/profile_control.py \
  --service vllm --action start_profile

python3 .agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py \
  --execution-id <id> --profile-root /vllm-workspace/.vaws-runtime/profiling
```
