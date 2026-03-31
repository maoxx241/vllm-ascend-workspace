---
name: workspace-bootstrap
description: Use when initializing or repairing this workspace from natural language user input, especially when the agent needs to gather server access details and vllm-ascend repo ownership before running internal bootstrap steps.
---

# Workspace Bootstrap

Use this skill when initializing or repairing the local workspace overlay for a real user.

- Keep the canonical runtime root at `/vllm-workspace`.
- Start from natural language user input, not by telling the user to invoke internal tools.
- Collect remote server connection details first.
- Collect `vllm-ascend` repo path or fork URL and Git auth second.
- Treat `vllm` fork configuration as optional.
- Once enough information is available, the agent should run `tools/vaws.py init --bootstrap ...` and then use `tools/vaws.py doctor` for follow-up checks.
- Treat `.workspace.local/` as local overlay state only.
- Keep tracked defaults on community `upstream` repositories and store user-specific `origin` topology in `.workspace.local/repos.yaml`.
- Prefer `config/*.example.yaml` as the public template source.
- Keep secrets, private hosts, and private paths out of tracked files.
- Guarded reset is a two-step flow: `tools/vaws.py reset --prepare` first, then `tools/vaws.py reset --execute --confirmation-id ... --confirm ...`.
- Agents must not skip prepare, reuse stale confirmation ids, or fabricate authorization.
- After a successful reset, restore `origin` and `upstream` on `vllm/` and `vllm-ascend/` to the community URLs.

This is workspace-local reference material only.
