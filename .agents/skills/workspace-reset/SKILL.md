---
name: workspace-reset
description: Use when resetting or deinitializing this workspace after an explicit user request, especially when the agent must clear local overlay identity and remote runtime state without skipping the guarded confirmation flow.
---

# Workspace Reset

Use this skill when a user explicitly asks to reset, deinitialize, or return this workspace to a near post-clone state.

- Keep the canonical runtime root at `/vllm-workspace`.
- Treat reset as a guarded two-step flow: run `tools/vaws.py reset --prepare` first, then `tools/vaws.py reset --execute --confirmation-id ... --confirm ...`.
- Show the destruction summary from `reset --prepare` before executing the destructive step.
- Agents must not skip prepare, reuse stale confirmation ids, or fabricate authorization.
- Reset is expected to clear local workspace identity and clean the approved remote runtime before local overlay cleanup.
- After a successful reset, restore `origin` and `upstream` on `vllm/` and `vllm-ascend/` to the community URLs.
- Keep private hosts, tokens, and user-specific paths out of tracked files.

This is workspace-local reference material only.
