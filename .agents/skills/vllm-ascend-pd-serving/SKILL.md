---
name: vllm-ascend-pd-serving
description: Plan, start, inspect, smoke-test, and stop a vLLM Ascend prefill/decode deployment as one coordinator topology execution. Use for PD disaggregation with NIXL, Mooncake, or another KV connector. Do not use for one colocated service, generic Ray clusters, correctness matrices, performance regression decisions, or distributed root-cause diagnosis.
---

# vllm-ascend-pd-serving

Start and inspect a prefill/decode deployment as one coordinator-owned topology.

Choose prefill/decode roles, parallelism, connector options and proxy routing from the deployment requirement. A successful HTTP response proves request handling; KV transfer needs connector-specific evidence.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py start --config topology.json
```

The business config contains services, connector, proxy and smoke workload; group_id and startup_order are optional. status and stop accept --service or --execution-id without a local lifecycle file. status may take --config for proxy health; smoke takes --config. Coordinator owns resource state and teardown.

Use ordinary serving for a colocated service. Route rank/connector hangs to distributed-debug.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)

- [Business input example](references/inputs.md)
