---
name: machine-management
description: Use when the user wants after setup or ongoing machine attach, verification, repair, or removal work for this workspace.
---

# Machine Management

## Overview

Manage machines after setup. Covers machine add, machine verify, and removal intent. Machine ready does not imply service ready.

## When to Use

### Intent Signals

- The user wants to attach a machine to this workspace.
- The user wants to verify whether a machine is ready.
- The user wants to remove a machine or its bound container.

### Examples Include, But Are Not Limited To:

- `把这台机器接进来`

## Quick Triage

- Inspect inventory first with `machine.describe_server` or `machine.list_servers`.
- Probe host access with `machine.probe_host_ssh` before deciding that repair is required.
- Probe container transport with `runtime.probe_container_transport` before assuming bootstrap is necessary.
- Keep `code_parity` and `runtime_env` as reusable-machine prerequisites, not as reasons to skip probe-first verification.
- Do not start at `.agents/discovery/README.md` when the verify or repair ladder is already named here.
- Do not read `tools/lib/*.py` until a named atomic tool fails and the stage is known.

## Default Recipe

- Inventory-first inspection: `machine.describe_server` or `machine.list_servers`
- Verify-first ladder: `machine.probe_host_ssh` -> `runtime.probe_container_transport`
- Repair ladder only when probes fail: `machine.bootstrap_host_ssh` -> `machine.sync_workspace_mirror` -> `runtime.reconcile_container` -> `runtime.bootstrap_container_transport`
- Removal path when the user explicitly wants cleanup: `runtime.cleanup_server`
- Detailed routing map: `references/internal-routing.md`
- Runtime bootstrap failure triage: `references/runtime-bootstrap-triage.md`

## Stop Conditions

- For verify-only requests, do not silently bootstrap.
- Stop on unexpected auth prompt.
- Stop on invalid credentials instead of looping more repair.
- Stop when repair would require risky destructive replacement that the user did not ask for.

## User-Visible Output Contract

- Report whether the requested machine is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Explain whether the machine is attached, reusable, or removed, keep the result framed as machine readiness, and make it explicit that machine ready does not imply service ready.

## Auth Boundary

- Allowed: one bare-metal password bootstrap when establishing host key-based access for a new machine.
- Forbidden: GitHub login prompts, repeated server password prompts after password bootstrap, and any container password prompt.
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
- Mixing verify and bootstrap until the user loses track of what mutated.

## Red Flags

- claiming a machine is ready without probe-first verification
- repairing automatically when the request was verify-only
- exposing inventory or secret details in public guidance
