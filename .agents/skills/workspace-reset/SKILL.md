---
name: workspace-reset
description: Use when the user explicitly wants destructive teardown, deinitialization, or a return to a near post-clone workspace state for this workspace.
---

# Workspace Reset

## Overview

Use this skill for explicit destructive teardown of workspace-managed state. It owns reset authorization, teardown reporting, and the difference between full cleanup and partial cleanup.

If exact internal routing details are required after this skill is selected, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user explicitly wants destructive teardown for this workspace.
- The user wants to deinitialize the workspace and return to a near post-clone state.
- The user wants setup traces removed before rebuilding the environment.

### Examples Include, But Are Not Limited To:

- `把这个 workspace 重置掉`
- `把环境回到刚 clone 的状态`
- `把初始化痕迹都清干净`
- `reset this workspace`

### Do Not Use

- Ordinary machine attach, verify, or removal work belongs to `machine-management`.
- First-time setup belongs to `workspace-init`.
- Benchmark execution belongs to `benchmark`.

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
- Clean workspace-managed machine state on a best-effort basis.
- Preserve explicit authorization even when the user-facing explanation stays high level.
- Treat partial cleanup as partial, not as success.

## Cross-Skill Boundary

- `workspace-init` owns first-time setup.
- `machine-management` owns ordinary machine attach, verify, and removal work.
- `benchmark` owns benchmark execution.
- `workspace-reset` owns explicit destructive teardown.

## Failure Handling Notes

- Never skip explicit authorization for destructive teardown.
- If cleanup is incomplete, report a partial result instead of pretending full success.
- Distinguish blocked teardown from reachable teardown that only completed partially.

## Security Notes

- Never fabricate authorization on the user's behalf.
- Never compress reset into a silent one-step action.
- Never expose raw secrets or private hosts in user-facing output.

## Common Mistakes

- Treating reset as routine maintenance.
- Explaining reset through overlay file mutations.
- Hiding unreachable cleanup outcomes.

## Red Flags

- asking the user for internal token syntax
- claiming success when teardown was only partial
- using reset language for ordinary maintenance work
