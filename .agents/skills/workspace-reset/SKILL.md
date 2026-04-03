---
name: workspace-reset
description: Use when the user explicitly wants destructive teardown, deinitialization, or a return to a near post-clone workspace state for this workspace.
---

# Workspace Reset

## Overview

Use this skill for high-friction destructive teardown of workspace-managed state. It owns destructive preview, explicit authorization, discovery-backed cleanup composition, and partial-cleanup reporting.

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

## Quick Triage

- Identify the destruction scope up front: `servers`, `auth`, `repos`, `known_hosts`, and local benchmark artifacts.
- Separate explicit destructive reset from targeted maintenance or normal removal work.
- Prepare a preview first with `reset.prepare_request`.
- Treat remote cleanup, overlay cleanup, known_hosts cleanup, and remote restoration as explicit separate steps.
- Treat unreachable remote cleanup as a reporting problem, not as permission to hide leftover state.

## Default Recipe

- Discovery family: `.agents/discovery/families/reset-cleanup.yaml`
- Preview and authorization step: `reset.prepare_request`
- Destructive cleanup sequence: `reset.cleanup_remote_runtime` -> `reset.cleanup_overlay` -> `reset.cleanup_known_hosts` -> `reset.restore_public_remotes`
- Execute teardown only after the preview and authorization are both complete.
- Report `partial` when cleanup finishes unevenly or some targets are unreachable.

## Stop Conditions

- Stop on missing authorization.
- Stop on unreachable cleanup that prevents full teardown, then report it as partial or blocked.
- Stop on any attempt to open a fresh auth flow during reset.

## User-Visible Output Contract

- Show a destruction summary before any destructive action.
- Ask for explicit authorization before teardown proceeds.
- Report the final result as `ready`, `partial`, `blocked`, or `failed`.
- Say plainly when some cleanup steps were unreachable or incomplete.

## Auth Boundary

- Allowed: none.
- Forbidden: any Git auth prompt, server auth prompt, or container auth prompt during reset work.
- On any unexpected auth prompt, fail closed as `blocked` or `failed` and do not open a new auth flow inside reset.

## Never Expose

- raw confirmation tokens or internal reset records as normal user guidance
- fabricated authorization
- raw private hosts, secrets, or internal cleanup paths

## Cross-Skill Boundary

- `workspace-init` owns first-time setup.
- `machine-management` owns ordinary machine attach, verify, and removal work.
- `serving` owns service lifecycle.
- `benchmark` owns benchmark execution.
- `workspace-reset` owns explicit destructive teardown.

## Common Mistakes

- Treating reset as routine maintenance.
- Collapsing preview and execution into one hidden step.
- Hiding incomplete cleanup outcomes.

## Red Flags

- asking the user for internal token syntax
- claiming success when teardown was only partial
- using reset language for ordinary maintenance work
