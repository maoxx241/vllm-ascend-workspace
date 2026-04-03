# Agent Tool Discovery

Start here when a public skill identifies the problem class but does not name the exact atomic tool to run.

## How To Use This Directory

1. Open `index.yaml`.
2. Match the current problem class to a family manifest.
3. Read the family manifest before running a tool.
4. Respect `action_kind` before execution:
   - `probe` means read-only
   - `repair`, `bootstrap`, `cleanup`, and `execute` mutate state or produce durable outputs
5. Use recipe metadata as advisory guidance, not hidden workflow authority.

## Current Scope

- `workspace-foundation` is the workspace-init discovery surface for overlay validity, auth, repo topology, submodules, and live repo targets.
- `workspace-diagnostics` is the pure diagnostics family behind `doctor`, covering overlay diagnosis and live workspace diagnosis without generic state residue checks.
- `machine-inventory` is the explicit inventory-only family for registering, inspecting, listing, and removing server records before runtime verification.
- `reset-cleanup` is the explicit destructive family for reset request recording, remote cleanup, overlay cleanup, known_hosts cleanup, and public remote restoration.
- `machine-runtime` is the Phase 1 reference family.
- `serving-lifecycle` is the Phase 3 reference family for service launch, readiness probing, inspection, and stop.
- `benchmark-execution` is the explicit benchmark family for preset inspection, existing-service benchmark execution, and result inspection.

## Namespace Rule

`vaws` is still present for compatibility, but it is not the discovery surface for new atomic tools and it must not gain new hidden workflows.
