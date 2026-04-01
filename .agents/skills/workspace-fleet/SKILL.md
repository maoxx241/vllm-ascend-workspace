---
name: workspace-fleet
description: Use when the user wants to manage additional servers after the first workspace baseline already exists, including attach, verify, list, or clearly blocked unsupported maintenance requests.
---

# Workspace Fleet

## Overview

Use this skill for post-bootstrap server inventory management. It owns the lifecycle of additional managed servers after the first baseline exists.

If exact internal routing details are required, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants to attach an additional managed server after baseline bootstrap.
- The user wants to verify or list a managed server.
- The user asks for post-bootstrap maintenance that may not have a sanctioned implementation path yet.
- The user wants to know whether a post-bootstrap server is usable for this workspace.

### Examples Include, But Are Not Limited To:

- `帮我配置一下 ip 为 b 的机器`
- `把 10.0.0.12 这台机器也接进来`
- `现在想把 server-b 也配到这个 workspace`

### Do Not Use

- The first baseline belongs to `workspace-bootstrap`.
- Explicit teardown belongs to `workspace-reset`.
- Session switching belongs to `workspace-session-switch`.

## User-Visible Output Contract

- Report whether the requested server operation is `ready`, `partial`, `needs_repair`, or `blocked`.
- Explain what changed in server terms.
- Keep read-only verification distinct from any state-changing request.
- Surface unsupported post-bootstrap maintenance requests as `blocked` and say plainly that no sanctioned implementation path exists yet.

## Never Expose

- local inventory file paths as user workflow steps
- raw auth handle names or secret-bearing values in ordinary guidance
- implementation-specific helper naming

## Default Inference Rules

- Reuse the single configured SSH auth profile when exactly one safe profile exists.
- Ask for clarification when multiple profiles exist and the correct one cannot be inferred safely.
- Treat verify as read-only.
- Do not improvise repair or removal behavior when no sanctioned implementation path exists.

## Cross-Skill Boundary

- First baseline work belongs to `workspace-bootstrap`.
- Explicit teardown belongs to `workspace-reset`.
- Session changes and sync do not belong to this skill.

## Failure Handling Notes

- If baseline bootstrap never completed, route back to `workspace-bootstrap`.
- If a server cannot be reached, return `needs_repair` or `blocked` instead of silently skipping it.
- If some requested changes succeed and others fail, return `partial`.
- If the user requests unsupported maintenance, return `blocked` instead of inventing a workflow.

## Security Notes

- Never ask the user to paste a password into a shell command.
- Never expose private hosts or tracked secrets in workflow guidance.
- Keep auth-ref resolution internal.

## Common Mistakes

- Treating fleet work as a bootstrap rerun.
- Explaining server inventory through YAML file edits.
- Improvising unsupported repair or removal behavior.

## Red Flags

- overfitting the skill to one literal “add machine” sentence
- leaking internal auth-ref vocabulary into user-facing guidance
- silently mutating server state during a verify-only request
- pretending remove or repair is supported when no sanctioned path exists
