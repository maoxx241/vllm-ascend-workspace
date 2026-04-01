---
name: workspace-fleet
description: Use when the user wants to attach, verify, list, or otherwise manage workspace servers after foundation and git-profile readiness exist, including first-server runtime handoff.
---

# Workspace Fleet

## Overview

Use this skill for managed server inventory and runtime handoff. It owns first-server attachment and later server attachment as the same fleet lifecycle once foundation and git-profile readiness are both complete.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants to attach an additional managed server after foundation and git-profile readiness exist.
- The user wants to verify or list a managed server.
- The user wants first server handoff after foundation and git-profile readiness is complete.
- The user wants to know whether a managed server is usable for this workspace.

### Examples Include, But Are Not Limited To:

- `帮我配置一下 ip 为 b 的机器`
- `把 10.0.0.12 这台机器也接进来`
- `现在想把 server-b 也配到这个 workspace`

### Do Not Use

- First-time orchestration belongs to `workspace-init`.
- Foundation readiness belongs to `workspace-foundation`.
- Git profile readiness belongs to `workspace-git-profile`.
- Explicit teardown belongs to `workspace-reset`.
- Session switching belongs to `workspace-session-switch`.

## User-Visible Output Contract

- Report whether the requested server operation is `ready`, `partial`, `needs_repair`, or `blocked`.
- Explain what changed in managed server terms.
- Keep read-only verification distinct from any state-changing request.
- Surface unsupported maintenance requests as `blocked` and say plainly that no sanctioned implementation path exists yet.

## Never Expose

- local inventory file paths as user workflow steps
- raw auth handle names or secret-bearing values in ordinary guidance
- implementation-specific helper naming

## Default Inference Rules

- Reuse the single configured SSH auth profile when exactly one safe profile exists.
- Ask for clarification when multiple profiles exist and the correct one cannot be inferred safely.
- Treat verify as read-only.
- Treat first-server attachment and later attachment through the same fleet ownership boundary.
- Do not improvise repair or removal behavior when no sanctioned implementation path exists.

## Cross-Skill Boundary

- First-time orchestration belongs to `workspace-init`.
- Local readiness belongs to `workspace-foundation`.
- Git topology belongs to `workspace-git-profile`.
- Explicit teardown belongs to `workspace-reset`.
- Session changes and sync do not belong to this skill.

## Failure Handling Notes

- If foundation or git-profile readiness never completed, route back to `workspace-init`.
- If a server cannot be reached, return `needs_repair` or `blocked` instead of silently skipping it.
- If some requested changes succeed and others fail, return `partial`.
- If the user requests unsupported maintenance, return `blocked` instead of inventing a workflow.

## Security Notes

- Never ask the user to paste a password into a shell command.
- Never expose private hosts or tracked secrets in workflow guidance.
- Keep auth-ref resolution internal.

## Common Mistakes

- Treating fleet work as an init rerun.
- Explaining server inventory through YAML file edits.
- Improvising unsupported repair or removal behavior.

## Red Flags

- overfitting the skill to one literal “add machine” sentence
- leaking internal auth-ref vocabulary into user-facing guidance
- silently mutating server state during a verify-only request
- pretending remove or repair is supported when no sanctioned path exists
