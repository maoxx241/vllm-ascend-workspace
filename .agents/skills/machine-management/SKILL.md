---
name: machine-management
description: Use when the user wants after setup or ongoing machine attach, verification, repair, or removal work for this workspace.
---

# Machine Management

## Overview

Manage machines for this workspace after setup. This skill is the public skill behind machine add, verify, repair, and remove intent, and machine ready does not imply service ready.

## When to Use

### Intent Signals

- The user wants to attach a machine to this workspace.
- The user wants to verify whether a machine is ready.
- The user wants to remove a machine or its bound container.
- The user wants ongoing machine attach or verification work after setup.

### Examples Include, But Are Not Limited To:

- `把这台机器接进来`
- `看这台机器现在能不能用`
- `把这台机器删掉`
- `attach a machine to this workspace`

## Quick Triage

- Inspect inventory first with `machine.describe_server` or `machine.list_servers`.
- Probe host access with `machine.probe_host_ssh` before deciding that repair is required.
- Probe container transport with `runtime.probe_container_transport` before assuming bootstrap is necessary.
- Keep `code_parity` and `runtime_env` as reusable-machine prerequisites, not as reasons to skip probe-first verification.
- If the real issue is service lifecycle rather than machine readiness, stop and hand off to `serving`.

## Default Recipe

- Discovery families: `.agents/discovery/families/machine-inventory.yaml` and `.agents/discovery/families/machine-runtime.yaml`
- Inventory-first inspection: `machine.describe_server` or `machine.list_servers`
- Verify-first ladder: `machine.probe_host_ssh` -> `runtime.probe_container_transport`
- Repair ladder only when probes fail: `machine.bootstrap_host_ssh` -> `machine.sync_workspace_mirror` -> `runtime.reconcile_container` -> `runtime.bootstrap_container_transport`
- Removal path when the user explicitly wants cleanup: `runtime.cleanup_server`

## Stop Conditions

- For verify-only requests, do not silently bootstrap.
- Stop on unexpected auth prompt.
- Stop on invalid credentials instead of looping more repair.
- Stop when repair would require risky destructive replacement that the user did not ask for.

## User-Visible Output Contract

- Report whether the requested machine is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Explain whether the machine is attached, reusable, or removed.
- Keep the result framed as machine readiness, not as raw inventory mutation.
- Make it explicit that machine ready does not imply service ready.

## Auth Boundary

- Allowed: one bare-metal password bootstrap when establishing host key-based access for a newly added machine.
- Forbidden: GitHub login prompts, repeated server password prompts after bootstrap, and any container password prompt.
- On any unexpected auth prompt, fail closed with `needs_input` or `needs_repair`.

## Never Expose

- raw passwords, tokens, or key material
- internal inventory records or lock paths
- private host metadata or storage-root internals unless the user explicitly asks

## Cross-Skill Boundary

- `workspace-init` owns first-time setup and optional first machine setup.
- `machine-management` owns later attach, verify, repair, and removal work.
- `serving` owns service lifecycle after the machine is ready.
- `benchmark` owns benchmark execution after the machine is ready.
- `workspace-reset` owns explicit destructive teardown.

## Common Mistakes

- Treating machine maintenance as a generic SSH wrapper.
- Re-attaching a machine instead of verifying an existing one.
- Mixing verify and bootstrap until the user loses track of what mutated.
- Treating a machine-ready result as if it also guaranteed service readiness.

## Red Flags

- claiming a machine is ready without probe-first verification
- repairing automatically when the request was verify-only
- exposing inventory or secret details in public guidance
