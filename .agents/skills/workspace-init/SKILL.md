---
name: workspace-init
description: Use when the user wants a first-time workspace baseline, staged re-initialization after reset, or recovery from a partially completed setup.
---

# Workspace Init

## Overview

Coordinate staged workspace initialization as one lifecycle, with foundation, git profile, and fleet handled as separate ownership boundaries.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants first-time initialization for this workspace.
- The user wants staged re-initialization after reset or partial failure.
- The user wants a first usable baseline that may still need follow-up lifecycle work.

### Examples Include, But Are Not Limited To:

- `先把这个 workspace 按 staged 流程跑起来`
- `把初始化拆成基础检查、Git 配置和 server 接入`
- `刚 reset 完，重新走一遍初始化`

### Do Not Use

- Local prerequisite checks without orchestration belong to `workspace-foundation`.
- Git identity or fork topology preparation belongs to `workspace-git-profile`.
- Managed server attachment belongs to `workspace-fleet`.

## User-Visible Output Contract

- Report whether initialization is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Explain whether the workspace now has a usable baseline.
- Say whether the outcome is fully staged, partially staged, or waiting on input.

## Never Expose

- internal overlay mutation steps
- raw secret values or secret-bearing handles
- private filesystem paths from lifecycle state

## Default Inference Rules

- Prefer remote-first when a usable server is supplied.
- Fall back to local-only only when remote attachment is unavailable or explicitly not requested.
- Treat first usable baseline and later server attachment as separate lifecycle responsibilities.

## Cross-Skill Boundary

- Foundation owns local prerequisite readiness.
- Git profile owns repository identity and fork topology.
- Fleet owns server attachment and runtime handoff.
- Reset owns destructive teardown.

## Failure Handling Notes

- Stop when required local or remote inputs are missing.
- Do not invent a first baseline if foundation or git profile is still incomplete.
- Route later server attachment work to fleet instead of retrying init as a separate runtime path.

## Security Notes

- Do not ask for pasted raw secrets when staged handles are available.
- Keep secret resolution internal to the workspace lifecycle.
- Never expose private hosts, tokens, or paths in the public contract.

## Common Mistakes

- Treating init as a monolithic runtime implementation.
- Folding repository topology or server attachment into one step.
- Reusing bootstrap wording as if it were a separate lifecycle.

## Red Flags

- routing first-server attachment outside fleet
- exposing secret or overlay details in public guidance
- claiming a usable baseline before the staged flow is complete
