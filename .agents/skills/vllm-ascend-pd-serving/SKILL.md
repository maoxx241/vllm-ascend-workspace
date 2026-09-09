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
- A service group from `session_group.py` (`members` are `name=service`).
- Connector type and options are already in each role's vLLM arguments.
- Proxy URL is already stable. Proxy process lifecycle is outside this skill.

## Workflow

1. `session_group.py create --group-id pd --member prefill=prefill --member decode=decode`
2. `pd_serving.py plan --config ... --group-file ...`
3. `pd_serving.py start` submits **one** topology execution (`service=<group_id>`).
   Queued / preparing / waiting is a truthful result with the same
   `execution_id`; do not resubmit.
4. `status` reads that execution and the proxy health path.
5. `smoke` posts the configured proxy request.
6. `stop` / group teardown calls coordinator `observe(stop)` on that execution.

## Entry point

`scripts/pd_serving.py` provides `plan`, `start`, `status`, `smoke`, and `stop`.

## Rules

- Never sequential `serve_start` per role.
- Never invent per-role recovery or lease reconstruction.
- Role commands come from the serving business command builder (`$VAWS_PYTHON`, `$VAWS_SERVICE_PORT`).
- Code identity is `manifest_code` from the native/package context, not a group snapshot field.
