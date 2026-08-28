# PD Serving behavior contract

## Config

The deployment references one ready Session Group. Each group member hosts at
most one vLLM service and each service declares:

- stable name and `prefill` or `decode` role;
- Session Group member;
- model, TP/DP, optional port and health timeout;
- environment and exact vLLM arguments.

At least one service of each role is required. `startup_order` contains every
service exactly once. Shutdown and rollback use the reverse order.

Planning validates every group member's name, session ID, and non-empty code
snapshot before creating lifecycle state. All member snapshots must match; a
malformed or mixed-snapshot group is rejected as input instead of failing later
while constructing the Run Manifest.

Connector type is `nixl`, `mooncake`, or `custom`. Connector options are recorded
for traceability, but the controller never synthesizes version-sensitive vLLM
arguments from them; exact connector CLI JSON remains explicit in each service.

## Proxy boundary

The MVP accepts an externally managed proxy or load-balancer URL. It verifies the
health endpoint and sends smoke requests through it, but does not own the proxy
process. This prevents generic process supervision from being duplicated inside
the Skill.

## Lifecycle

- `plan`: immutable config, group snapshot, lifecycle, state, and Run Manifest;
- `start`: ordered single-node Serving calls with reverse rollback;
- `status`: every member service plus proxy health;
- `smoke`: one configured proxy request and raw response;
- `stop`: reverse-order member stop.

A successful proxy request proves the routed request path. Connector-level KV
transfer requires corroborating service logs or connector metrics.
