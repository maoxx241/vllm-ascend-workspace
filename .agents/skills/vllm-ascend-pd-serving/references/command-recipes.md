# PD Serving command recipes

## Plan

```bash
python -B .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py plan \
  --output-dir .vaws-local/pd-serving/case-001 \
  --config /path/to/pd-config.json
```

`group_id` is the business name passed to coordinator; service names identify
roles within that execution. A config has the following shape (replace model,
connector arguments and proxy URL with the requested deployment):

```json
{
  "schema_version": 1,
  "run_id": "pd-case-001",
  "group_id": "pd",
  "connector": {"type": "custom", "options": {}},
  "services": [
    {"name": "decode", "role": "decode", "model": "/models/example", "tp": 1, "args": []},
    {"name": "prefill", "role": "prefill", "model": "/models/example", "tp": 1, "args": []}
  ],
  "startup_order": ["decode", "prefill"],
  "proxy": {"base_url": "http://127.0.0.1:9000", "health_path": "/health"},
  "smoke": {"path": "/v1/completions", "request": {"model": "example", "prompt": "hello"}}
}
```

`args` carries the actual connector configuration; the example is a config
shape, not evidence that an unconfigured connector transfers KV. Supply native
`--context-file` when it is not already available through the client hook.

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
  --output-dir .vaws-local/pd-serving/case-001
```

If the result is `stopping`, release is asynchronous. Repeat the same
command until `stopped`; the manifest is completed only after release.
