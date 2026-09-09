# PD Serving behavior contract

## Config

The deployment references one service group whose members are `name` plus
task-scoped `service` names. Code identity comes from the native task /
`manifest_code`, not a per-member snapshot field.

Each service declares:

- stable name and `prefill` or `decode` role;
- group member;
- model, TP/DP, optional port, host, and health timeout;
- environment and exact vLLM arguments (connector JSON included).

At least one service of each role is required. `startup_order` names every
service once. That order is only the topology role list; the coordinator
reserves the full group before any role `go`.

Connector type is `nixl`, `mooncake`, or `custom`. The controller never
synthesizes connector CLI from type/options.

## Proxy boundary

The MVP accepts an externally managed proxy URL. It verifies health and smoke
through that URL. It does not own the proxy process.

## Lifecycle

- `plan`: config, group, topology roles, state, Run Manifest
- `start`: one `TaskClient.run(topology=..., service=<group_id>, timeout_seconds=None)`
- queued / preparing / waiting is a truthful status with the same `execution_id`
- `status`: that execution plus proxy health; `observation.roles[]` carries per-role target/tail
- `smoke`: one configured proxy request
- `stop`: `observe(execution_id, "stop")` for that same execution

A successful proxy request proves the routed request path. Connector-level KV
transfer requires corroborating service logs.
