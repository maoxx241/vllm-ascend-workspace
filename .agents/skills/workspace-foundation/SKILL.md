---
name: workspace-foundation
description: Use when the user needs local prerequisite checks, control-plane readiness, or recovery from missing optional tooling before staged workspace initialization.
---

# Workspace Foundation

## Overview

Handle local readiness checks for the workspace control plane before any staged initialization or server attachment work continues.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants local prerequisite checks.
- The user wants to know whether the control plane is ready, degraded, or blocked.
- The user is missing optional tooling and needs a readiness judgment before init continues.
- The user wants foundation checks before staged setup continues.

### Examples Include, But Are Not Limited To:

- `先看一下本地依赖是不是齐了`
- `把控制平面健康状态确认一下`
- `先确认现在能不能继续 staged 初始化`

### Do Not Use

- Full workspace orchestration belongs to `workspace-init`.
- Git identity and fork topology belong to `workspace-git-profile`.
- Managed server attachment belongs to `workspace-fleet`.

## User-Visible Output Contract

- Report whether foundation is `ready`, `degraded`, or `blocked`.
- Explain which prerequisite class is missing or only recommended.
- Keep the result readable as a readiness judgment, not a shell transcript.

## Never Expose

- exact dependency probe commands
- raw environment values or secret handles
- private file paths from local readiness state

## Default Inference Rules

- Treat missing required local tools as blocked.
- Treat missing recommended tooling as degraded.
- Prefer to continue the staged flow only when the readiness result is acceptable for the requested path.

## Cross-Skill Boundary

- Init owns orchestration across the staged lifecycle.
- Git profile owns repository topology and auth-ready remotes.
- Fleet owns server attachment and runtime verification.
- Doctor only reports repository and overlay health, not staged lifecycle policy.

## Failure Handling Notes

- Stop if a required prerequisite is missing.
- Preserve a degraded result when only recommended tooling is absent.
- Do not convert control-plane readiness into server lifecycle work.

## Security Notes

- Never request raw secrets for local readiness checks.
- Avoid leaking local environment details that are not needed for the readiness judgment.

## Common Mistakes

- Treating optional tooling as a hard blocker.
- Moving init or fleet ownership into foundation.
- Explaining readiness through implementation details instead of the result.

## Red Flags

- leaking command syntax into public readiness guidance
- calling server attachment a foundation concern
- hiding degraded readiness behind a generic success message
