# Agent call

From the repository root:

```text
python .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py start --config topology.json
```

The business config contains services, connector, proxy and smoke workload; group_id and startup_order are optional. status and stop accept --service or --execution-id without a local lifecycle file. status may take --config for proxy health; smoke takes --config. Coordinator owns resource state and teardown.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
