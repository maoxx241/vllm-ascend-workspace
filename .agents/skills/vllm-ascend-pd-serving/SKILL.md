---
name: vllm-ascend-pd-serving
description: Plan, start, inspect, smoke-test, and stop a vLLM Ascend prefill/decode deployment as one coordinator topology execution. Use for PD disaggregation with NIXL, Mooncake, or another KV connector. Do not use for one colocated service, generic Ray clusters, correctness matrices, performance regression decisions, or distributed root-cause diagnosis.
---

# vLLM Ascend PD Serving

One coordinator `TaskClient.run(topology=...)` admits every PD role. This
skill owns connector configuration, proxy health, smoke, and reporting.
It does not allocate NPUs, launch roles one-by-one, or roll back a partial
group — the package reserves the full group before any role starts.

## Preconditions

- Native task context (`--context-file` / `VAWS_CONTEXT_FILE`).
- One PD config with a task-scoped business `group_id` and the complete role list.
- Connector type and options are already in each role's vLLM arguments.
- Proxy URL is already stable. Proxy process lifecycle is outside this skill.

## Workflow

1. `pd_serving.py plan --config ...` validates the business config and records its topology.
2. `pd_serving.py start` submits **one** topology execution (`service=<group_id>`).
   Queued / preparing / waiting is a truthful result with the same
   `execution_id`; do not resubmit.
3. `status` reads that execution and the proxy health path.
4. `smoke` posts the configured proxy request.
5. `stop` calls coordinator `observe(stop)` on that execution.

Read [command recipes](references/command-recipes.md) for the config shape.

## Entry point

`scripts/pd_serving.py` provides `plan`, `start`, `status`, `smoke`, and `stop`.

## Rules

- Never sequential `serve_start` per role.
- Never invent per-role recovery or lease reconstruction.
- Role commands come from the serving business command builder (`$VAWS_PYTHON`, `$VAWS_SERVICE_PORT`).
- Code identity is `manifest_code` from the native/package context, not a group snapshot field.
