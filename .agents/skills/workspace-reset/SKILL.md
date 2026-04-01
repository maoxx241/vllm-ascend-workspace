---
name: workspace-reset
description: Use when the user explicitly wants destructive teardown, deinitialization, or a return to a near post-clone workspace state after lifecycle-managed setup exists.
---

# Workspace Reset

## Overview

Use this skill for explicit guarded destructive teardown in the workspace lifecycle. It owns reset semantics, authorization friction, and lifecycle-owned cleanup reporting.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user explicitly wants this workspace reset or deinitialized.
- The user wants to clear lifecycle state and managed runtime state.
- The user wants a near post-clone state for repeated bootstrap testing.

### Examples Include, But Are Not Limited To:

- `把这个 workspace 重置掉`
- `把环境回到刚 clone 的状态`
- `把现在这套 bootstrap 痕迹都清干净`

### Do Not Use

- Ordinary server management belongs to `workspace-fleet`.
- First setup belongs to `workspace-init`.
- Session switching belongs to `workspace-session-switch`.

## User-Visible Output Contract

- Show a destruction summary before any destructive action.
- Ask for explicit authorization before teardown proceeds.
- Report the final result as `ready`, `partial`, `blocked`, or `failed`.
- Say plainly when some cleanup steps were unreachable or incomplete.

## Never Expose

- raw confirmation tokens or internal reset records as normal user guidance
- fabricated authorization
- raw private hosts, secrets, or internal cleanup paths

## Default Inference Rules

- Treat reset as high-friction by default.
- Clean all managed servers on a best-effort basis.
- Preserve the guarded two-phase internal flow even when the user-facing explanation stays high level.
- Treat lifecycle-owned managed-server cleanup as part of reset, not fleet.

## Cross-Skill Boundary

- First setup belongs to `workspace-init`.
- Post-bootstrap server maintenance belongs to `workspace-fleet`.
- Session switching belongs to `workspace-session-switch`.

## Failure Handling Notes

- Never skip the internal prepare phase.
- Never reuse stale authorization state.
- If cleanup is incomplete, report a partial result instead of pretending full success.

## Security Notes

- Never fabricate authorization on the user's behalf.
- Never compress reset into a silent one-step action.
- Never expose raw secrets or private hosts in user-facing output.

## Common Mistakes

- Treating reset as routine cleanup.
- Explaining reset through overlay file mutations.
- Hiding unreachable cleanup outcomes.

## Red Flags

- asking the user for internal token syntax
- claiming success when teardown was only partial
- using reset language for ordinary maintenance work
