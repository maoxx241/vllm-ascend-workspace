---
name: machine-management
description: Use when the user wants to attach a machine, verify whether a machine is ready, or remove a machine from this workspace.
---

# Machine Management

## Overview

Manage machines for this workspace after first-time setup. This skill handles machine attach, readiness verification, and removal without exposing inventory internals or backend transport details.

If exact internal routing details are required after this skill is selected, see `references/internal-routing.md`.

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

### Do Not Use

- First-time Git setup and first-machine setup belong to `workspace-init`.
- Benchmark execution belongs to `benchmark`.
- Destructive teardown belongs to `workspace-reset`.

## User-Visible Output Contract

- Report whether the requested machine is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Explain whether the machine is attached, reusable, or removed.
- Keep the result framed as workspace machine readiness, not as raw inventory file editing.

## Never Expose

- raw passwords, tokens, or key material
- internal inventory records or lock paths
- private host metadata or storage-root internals unless the user explicitly asks

## Default Inference Rules

- Reuse an already attached and ready machine when it matches the request.
- Prefer steady-state public-key access after first attachment succeeds.
- Treat verification and repair as part of the same maintenance capability when the machine is already known.

## Cross-Skill Boundary

- `workspace-init` owns first-time Git setup and optional first-machine setup.
- `machine-management` owns later machine attach, verify, and removal work.
- `benchmark` owns benchmark execution after the machine is ready.
- `workspace-reset` owns explicit destructive teardown.

## Failure Handling Notes

- Stop when required machine input is missing or unsafe to infer.
- Do not claim a machine is ready if runtime verification fails.
- When removal is partial, report what remains instead of pretending success.

## Security Notes

- Never ask the user to paste raw credentials into the transcript when an existing workspace secret reference should be used.
- Keep machine auth transitions and key installation inside the workspace boundary.
- Never expose private hosts, tokens, or key material in public guidance.

## Common Mistakes

- Treating machine maintenance as a generic SSH command wrapper.
- Re-attaching a machine instead of verifying or repairing an existing one.
- Explaining machine readiness in terms of raw state files or container internals.

## Red Flags

- claiming a machine is ready without runtime verification
- exposing inventory or secret details in public guidance
- routing routine machine maintenance back through first-time setup
