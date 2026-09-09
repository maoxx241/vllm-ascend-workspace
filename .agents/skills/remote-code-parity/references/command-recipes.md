# Command recipes

```bash
python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --host 10.0.0.1 --print-derived-args

python3 .agents/skills/remote-code-parity/scripts/parity_sync.py \
  --host 10.0.0.1 --runtime-root /vllm-workspace --dry-run

python3 .agents/scripts/remote_sync_plan.py --host 10.0.0.1 --mode source-only
python3 .agents/scripts/remote_sync_apply.py --host 10.0.0.1 --mode source-only
```

`--execution-id` is refused. Do not restore `install_consent.py`.
