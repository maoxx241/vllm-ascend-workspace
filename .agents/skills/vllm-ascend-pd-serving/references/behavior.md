# PD Serving behavior contract

## Config

The deployment config contains a task-scoped `group_id` (the business name
of the one coordinator execution) and its services. No separate group registry
or group file is required. Code identity comes from the native task /
`manifest_code`; the config does not grant a task identity.

Each service declares:

- stable name and `prefill` or `decode` role;
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

- `plan`: config, topology roles, state, Run Manifest
- `start`: one `TaskClient.run(topology=..., service=<group_id>, timeout_seconds=None)`
- queued / preparing / waiting is a truthful status with the same `execution_id`
- `status`: synchronizes queued/running/terminal execution facts into state and manifest; `roles[]` is a compact role summary, and target/tail remain available through coordinator execution tools.
- `smoke`: one configured proxy request
- `stop`: `observe(execution_id, "stop")` for that same execution

A successful proxy request proves the routed request path. Connector-level KV
transfer requires corroborating service logs.

Readiness is independent of lifecycle. Not-yet-listening is `starting`; a service that was ready and loses health is `unhealthy`. Terminal and stopping executions do not probe HTTP. Proxy mode defaults to `direct`; `environment` explicitly uses process proxy settings, with HTTP status and connection failure classification in the response.

State stores business references and readiness, not full coordinator responses, assignments, or duplicated role targets. A completed manifest never regresses. Stopping requires coordinator `resources_released`; `passed` additionally requires a passed smoke.
