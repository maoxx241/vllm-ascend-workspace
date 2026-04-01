---
name: workspace-bootstrap
description: Use when older workspace guidance still mentions bootstrap or asks to route that intent into workspace-init.
---

# Workspace Bootstrap

## Overview

Compatibility alias for legacy bootstrap wording. It forwards bootstrap intent to `workspace-init` semantics instead of owning a separate lifecycle path.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user still says bootstrap.
- Older workspace guidance refers to staged init with bootstrap language.
- A compatibility shim is needed for legacy prompts or docs.

### Examples Include, But Are Not Limited To:

- `这个老流程里还在说 bootstrap`
- `把旧版 workspace 初始化说法接过来`
- `按兼容方式理解这次初始化`

### Do Not Use

- First-time orchestration belongs to `workspace-init`.
- Local readiness belongs to `workspace-foundation`.
- Git profile belongs to `workspace-git-profile`.

## User-Visible Output Contract

- Report the request using `workspace-init` semantics.
- Explain that this is a compatibility alias when relevant.
- Keep the response centered on the actual lifecycle outcome, not on the alias itself.

## Never Expose

- internal overlay mutation steps
- raw secret values or secret-bearing handles
- private filesystem paths from lifecycle state

## Default Inference Rules

- Prefer the staged init interpretation when the request is ambiguous.
- Keep legacy bootstrap wording mapped to first-time initialization semantics.
- Treat later server attachment as fleet-owned, not bootstrap-owned.

## Cross-Skill Boundary

- `workspace-init` owns staged workspace orchestration.
- `workspace-foundation` owns local prerequisite readiness.
- `workspace-git-profile` owns repository topology and auth-ready remotes.
- `workspace-fleet` owns managed server attachment.

## Failure Handling Notes

- Do not invent a separate bootstrap runtime path.
- Route missing local readiness to foundation instead of expanding the alias.
- Route server attachment work to fleet.

## Security Notes

- Do not ask for pasted raw secrets when staged handles are available.
- Keep secret resolution internal to the workspace lifecycle.
- Never expose private hosts, tokens, or paths in the public contract.

## Common Mistakes

- Treating the alias as a separate lifecycle.
- Reintroducing bootstrap-era wording as if it were first-class.
- Folding server attachment or repo topology into the alias.

## Red Flags

- routing first-time setup away from `workspace-init`
- exposing secret or overlay details in public guidance
- claiming a bootstrap-specific runtime path exists
