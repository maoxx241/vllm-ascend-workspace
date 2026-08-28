# PD Serving command recipes

## Plan

```bash
python -B .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py plan \
  --output-dir .vaws-local/pd-serving/case-001 \
  --config /path/to/pd-config.json \
  --group-file .vaws-local/sessions/groups/pd-group/group.json
```

## Start and inspect

```bash
python -B .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py start \
  --output-dir .vaws-local/pd-serving/case-001

python -B .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py status \
  --output-dir .vaws-local/pd-serving/case-001
```

## KV request-path smoke

```bash
python -B .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py smoke \
  --output-dir .vaws-local/pd-serving/case-001
```

Inspect both role logs before claiming connector-level KV transfer.

## Stop

```bash
python -B .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py stop \
  --output-dir .vaws-local/pd-serving/case-001 \
  --force
```
