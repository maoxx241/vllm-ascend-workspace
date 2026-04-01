---
name: workspace-session-switch
description: Use when the user wants to create, change, or inspect the active feature session for this workspace after fleet-owned handoff or a compatible legacy target handoff exists.
---

# Workspace Session Switch

## Overview

Use this skill for active feature-session lifecycle semantics. It owns session creation, session switching, and “which session am I on” requests after fleet has established the current target or a compatible legacy target handoff.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants to create a new feature session.
- The user wants to switch the active session.
- The user wants to know which session is currently active.
- The user has a fleet-owned current target or a compatible legacy target handoff and wants session lifecycle work.

### Examples Include, But Are Not Limited To:

- `给我新建一个 feature session`
- `看下现在在哪个 session`
- `把当前工作切到 feat_x`

### Do Not Use

- First setup belongs to `workspace-init`.
- Server inventory work belongs to `workspace-fleet`.
- Repository or session sync belongs to `workspace-sync`.

## User-Visible Output Contract

- Report whether the requested session action is `ready`, `blocked`, or `failed`.
- Explain which session is now active, or why the requested session action could not complete.

## Never Expose

- local manifest file paths as public workflow steps
- raw local environment internals unrelated to the user’s requested session action

## Default Inference Rules

- Reuse the requested session name when it is safe and explicit.
- Ask for clarification when the target session name is ambiguous.
- Treat the fleet-owned current target or a compatible legacy target handoff as the session-switch precondition.

## Cross-Skill Boundary

- Init belongs to `workspace-init`.
- Fleet server management belongs to `workspace-fleet`.
- Sync belongs to `workspace-sync`.
- Destructive teardown belongs to `workspace-reset`.

## Failure Handling Notes

- Reject unsafe or invalid session names clearly.
- If the target session does not exist, explain whether creation is required.
- If the current target is missing, say that fleet handoff or compatible legacy target handoff is required first.

## Security Notes

- Never expose private environment details from local session manifests.
- Never treat session switching as a place to leak internal runtime paths.

## Common Mistakes

- Explaining session changes through manifest file edits.
- Collapsing creation and switching into one unclear workflow.

## Red Flags

- telling the user to edit local session state manually
- treating sync or reset as part of ordinary session switching
