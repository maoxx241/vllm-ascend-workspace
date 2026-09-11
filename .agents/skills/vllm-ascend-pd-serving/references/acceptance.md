# PD Serving acceptance

## Business input

- The config alone defines unique service names, both prefill/decode roles,
  model/parallelism, connector arguments and proxy request. No group file is
  required and no task context is inferred from saved config.
- Invalid or duplicate roles/names fail before lifecycle files are created.
- The role list contains every service once. Actual code identity is recorded
  through the coordinator contract.

## Managed execution

- Start submits one topology run containing every role and its exact command,
  environment and resource requirements. Coordinator owns admission, startup
  failure handling and resource release.
- Queued/preparing is not running. Status and stop address the same owned
  execution; proxy health is only checked for a running deployment.
- One start followed by status advances State and Run Manifest. The workspace does not create
  another registry or per-role allocation/rollback loop.

## Smoke

- The request goes through the proxy, and its response is preserved.
- Service logs or connector metrics corroborate KV transfer before that claim.
- Correctness and performance conclusions require their own business evidence.

- Stale observations cannot reopen terminal manifests. No health probe occurs after termination.
- `resources_released` is required before stop completes; stop alone does not prove inference passed.
- Actual remote CLI parser rejects invalid arguments before any role receives an NPU lease.
- Default output excludes duplicated full observations and environment/preamble data.
