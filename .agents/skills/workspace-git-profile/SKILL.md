---
name: workspace-git-profile
description: Use when the user needs Git identity, fork topology, or repo remotes prepared for a personalized workspace.
---

# Workspace Git Profile

## Overview

Handle repository identity and fork topology for a personalized workspace, including the remotes and auth references needed by later lifecycle steps.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants Git identity prepared for this workspace.
- The user wants repository remotes or fork topology normalized.
- The user wants a personalized workspace setup rather than community defaults.
- The user wants repo remotes ready for a personalized git setup.

### Examples Include, But Are Not Limited To:

- `把这套仓库的来源和身份收口`
- `先整理 Git 侧的个人化配置`
- `让我用自己的 fork 关系继续`

### Do Not Use

- Local prerequisite readiness belongs to `workspace-foundation`.
- Staged orchestration belongs to `workspace-init`.
- Managed server attachment belongs to `workspace-fleet`.

## User-Visible Output Contract

- Report whether git profile setup is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Explain which repository identity or fork inputs are missing.
- Keep the result framed as topology and auth readiness, not as raw config editing.

## Never Expose

- raw token values or key material
- internal remotes or auth refs that the user did not ask for
- private overlay paths or file contents

## Default Inference Rules

- Prefer a personalized topology when the workspace already has explicit origin information.
- Ask for missing origin input instead of silently falling back to community defaults.
- Reuse existing ready topology when it still matches the requested workspace identity.

## Cross-Skill Boundary

- Foundation owns local prerequisite readiness.
- Init owns orchestration across staged setup.
- Fleet owns runtime handoff after git profile is ready.
- Reset owns cleanup of repository identity and server state.

## Failure Handling Notes

- Stop when the workspace needs origin input that cannot be inferred safely.
- Treat partially populated topology as needing input or repair, not as ready.
- Do not move server attachment work into git profile.

## Security Notes

- Never ask the user to paste raw credentials into the transcript.
- Keep secret-bearing auth material internal to the workspace overlay.
- Do not expose private hosts or private repository paths in public guidance.

## Common Mistakes

- Treating git profile as a generic git command wrapper.
- Falling back to community defaults when personalization is required.
- Collapsing remote topology into server attachment.

## Red Flags

- exposing auth ref internals in public guidance
- pretending repo remotes are ready without a personalized topology
- routing fleet or init work into git profile
