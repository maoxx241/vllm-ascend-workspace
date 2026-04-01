---
name: workspace-sync
description: Use when the user wants to check sync status, start or finish the session-oriented compatibility sync flow, or discuss sync around an active session without internal command wiring.
---

# Workspace Sync

## Overview

Use this skill for session-oriented compatibility sync semantics in the workspace lifecycle. It owns the user-facing meaning of sync status, sync start, and sync done, not the low-level command mapping.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants to check current sync status.
- The user wants to start the compatibility sync flow for a session.
- The user wants to finish the compatibility sync flow cleanly.
- The user wants to sync repository or session state around the active session.

### Examples Include, But Are Not Limited To:

- `帮我同步一下`
- `看下当前 sync 状态`
- `给 feat_x 开始 sync`

### Do Not Use

- Init belongs to `workspace-init`.
- Additional server management belongs to `workspace-fleet`.
- Session creation or switching belongs to `workspace-session-switch`.
- Destructive teardown belongs to `workspace-reset`.
- Requests framed as generic repository or session state sync must be narrowed to the session-oriented compatibility flow before proceeding.

## User-Visible Output Contract

- Report whether sync is current, started, completed, blocked, or needs follow-up.
- Explain the outcome in session or compatibility-flow terms, not overlay-file terms.

## Never Expose

- exact low-level sync command routing
- overlay file paths as public workflow steps
- secret-bearing environment setup

## Default Inference Rules

- Prefer the current active session when status or done can be resolved safely from workspace state.
- Ask for clarification when a sync start request does not identify the target session clearly.
- Treat sync start as session-oriented compatibility flow, not as bootstrap or server attachment.

## Cross-Skill Boundary

- Init belongs to `workspace-init`.
- Server management belongs to `workspace-fleet`.
- Session selection belongs to `workspace-session-switch`.
- Teardown belongs to `workspace-reset`.

## Failure Handling Notes

- If sync cannot proceed, report the blocking reason plainly.
- If sync is partial, say what remains unresolved.
- If a sync start request lacks a usable session target, stop and ask instead of improvising.
- If no current session exists, use the active session or target selection skills first.

## Security Notes

- Never ask the user to paste secrets into shell commands.
- Never expose private remotes or private hosts in tracked docs.

## Common Mistakes

- Treating sync as a bootstrap substitute.
- Explaining sync through internal file mutations.

## Red Flags

- telling the user to run internal sync commands directly during normal guidance
- treating sync as a destructive reset
