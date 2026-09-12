# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py start --config topology.json
```

The business config contains services, connector, proxy and smoke workload; group_id and startup_order are optional. status and stop accept --service or --execution-id without a local lifecycle file. status may take --config for proxy health; smoke takes --config. Coordinator owns resource state and teardown.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
