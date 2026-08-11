---
name: vllm-ascend-pd-serving
description: Plan, start, inspect, smoke-test, and stop a multi-session vLLM Ascend prefill/decode deployment with explicit connector configuration, role ordering, proxy endpoint health, rollback, and KV-transfer request evidence. Use for PD disaggregation with NIXL, Mooncake, or another KV connector. Do not use for one colocated service, generic Ray clusters, correctness matrices, performance regression decisions, or distributed root-cause diagnosis.
---

# vLLM Ascend PD Serving

Operate one prefill/decode deployment across an existing Session Group whose
members have distinct session IDs. Reuse
single-node Serving for each vLLM process; this Skill owns only cross-service
role configuration, ordering, proxy health, rollback, and end-to-end smoke.

## Preconditions

- Every service has its own ready session.
- `session-management` has grouped those sessions and proved an identical code
  plus submodule snapshot.
- Connector type and options are explicit in the config.
- The proxy or load balancer already has a stable URL. Proxy process lifecycle
  remains outside this MVP; its health and request path are verified here.

## Workflow

1. Run `scripts/pd_serving.py plan` with a PD config and Session Group file.
2. Review generated commands. Connector options must already be present in each
   role's vLLM arguments; the controller never invents connector metadata.
3. Run `start`. Services launch in declared order through the existing
   single-node Serving entry point.
4. If any role fails, the controller stops already-started roles in reverse
   order and returns a failed state.
5. Run `status` to inspect every member and the proxy health endpoint.
6. Run `smoke` to send the configured request through the proxy and preserve the
   response as KV-transfer path evidence.
7. Run `stop`; roles stop in reverse startup order.

## Entry point

`scripts/pd_serving.py` provides `plan`, `start`, `status`, `smoke`, and `stop`.

Read only the reference needed for the active phase:

- [Behavior contract](references/behavior.md)
- [Command recipes](references/command-recipes.md)
- [Acceptance](references/acceptance.md)

## Boundaries

- Single-node start/status/stop belongs to `vllm-ascend-serving`.
- Session creation, leases, and group teardown belong to `session-management`.
- A working PD deployment's accuracy or performance belongs to the corresponding
  validation workflow.
- Hangs, rank divergence, endpoint mismatch, or connector diagnosis after a
  stable reproduction belongs to `vllm-ascend-distributed-debug`.

## Rules

- Never mix code snapshots inside one deployment.
- Never start a role outside the declared order.
- Always rollback already-started roles after a partial failure.
- Do not report KV transfer as proven from health checks alone; require a proxy
  request response and retain raw service logs for deeper confirmation.
- Keep state under `.vaws-local/pd-serving/`.
