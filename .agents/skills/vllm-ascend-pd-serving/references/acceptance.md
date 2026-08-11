# PD Serving acceptance

## Preconditions

- [ ] Session Group is ready and all members share one code/submodule snapshot.
- [ ] Missing member names, session IDs, or snapshots fail validation before lifecycle files are created.
- [ ] Distinct group members cannot alias the same session ID.
- [ ] Every service uses a unique group member.
- [ ] Prefill and decode roles both exist.
- [ ] Connector type, options, role-specific CLI JSON, endpoints, and ports are explicit.

## Lifecycle

- [ ] Startup follows the declared order.
- [ ] Partial startup failure rolls back started roles in reverse.
- [ ] Status covers every vLLM service and the proxy health endpoint.
- [ ] Stop covers every role in reverse startup order.
- [ ] State and Run Manifest retain all lifecycle results.

## Smoke

- [ ] Request enters through the proxy rather than a role's direct endpoint.
- [ ] HTTP status and response body are preserved.
- [ ] Service logs or connector metrics corroborate KV transfer before making that claim.
- [ ] Correctness and performance conclusions are produced by their own workflows.
