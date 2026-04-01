---
name: workspace-bootstrap
description: Use when the user wants the first usable baseline for this workspace, needs the first development server attached, or needs to recover an incomplete first bootstrap.
---

# Workspace Bootstrap

## Overview

Use this skill for the first usable workspace baseline. It owns bootstrap intent and bootstrap-facing user semantics for the workspace lifecycle.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants to initialize this workspace for the first time.
- The user wants the first development server attached to this workspace.
- The user wants a local-only baseline because no remote server is available.
- The user wants to recover an incomplete first baseline.

### Examples Include, But Are Not Limited To:

- `帮我初始化这个仓库`
- `先把这个 workspace 跑起来`
- `没有远端机器，先本地初始化`

### Do Not Use

- Adding or repairing later servers belongs to `workspace-fleet`.
- Explicit teardown belongs to `workspace-reset`.
- Session switching belongs to `workspace-session-switch`.

## User-Visible Output Contract

- Report whether the workspace is `ready`, `needs_input`, `blocked`, or `needs_repair`.
- Explain whether a first usable baseline now exists.
- Say whether the result is remote-first or local-only in user language.

## Never Expose

- overlay file paths as public workflow steps
- raw password values, raw token values, or secret-bearing shell commands
- private hosts or private local filesystem paths

## Default Inference Rules

- Prefer remote-first when the user provides a server.
- Fall back to local-only only when the user explicitly wants it or has no remote server.
- Treat `vllm-ascend` origin ownership as required for personalized development.
- Treat `vllm` origin ownership as optional.

## Cross-Skill Boundary

- Post-bootstrap server inventory work belongs to `workspace-fleet`.
- Explicit teardown belongs to `workspace-reset`.
- Feature session changes belong to `workspace-session-switch`.

## Failure Handling Notes

- If the first baseline already exists, route to `workspace-fleet` instead of retrying bootstrap blindly.
- If required auth or ownership information is missing, return `needs_input`.
- If runtime verification is incomplete, return `needs_repair`.

## Security Notes

- Secret handles must be pre-staged outside the agent transcript path.
- Never ask the user to echo or paste secrets into shell commands.
- Never expose private hosts or private paths in tracked docs.

## Common Mistakes

- Treating later machine additions as bootstrap work.
- Explaining bootstrap through internal files or command flags.
- Claiming bootstrap is ready before a first usable baseline actually exists.

## Red Flags

- explaining bootstrap with CLI syntax instead of lifecycle semantics
- routing a post-bootstrap machine request back into bootstrap
- asking the user to edit local overlay files directly
