# CLAUDE

This repository keeps its workflow contract inside the workspace. Do not rely on user-global skill installation for the workspace lifecycle.

## Always-On Context

- Runtime root: `/vllm-workspace`
- Primary implementation surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup`, `./sync`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- `.workspace.local/` is local-only overlay state and must never be committed

## Workflow Routing

- Route first baseline initialization to `.agents/skills/workspace-bootstrap/SKILL.md`
- Route post-bootstrap server management to `.agents/skills/workspace-fleet/SKILL.md`
- Route destructive teardown to `.agents/skills/workspace-reset/SKILL.md`
- Route session lifecycle changes to `.agents/skills/workspace-session-switch/SKILL.md`
- Route sync status and compatibility sync flow to `.agents/skills/workspace-sync/SKILL.md`

## Adapter Boundary

- Treat shared `SKILL.md` files as the public contract layer
- Treat skill-local routing references as internal execution notes only
