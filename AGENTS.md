# AGENTS

This repository is a public-facing control workspace for vLLM Ascend development workflows.

## Always-On Context

- Runtime root: `/vllm-workspace`
- Primary implementation surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup`, `./sync`
- Real-host acceptance entrypoint: `python tools/vaws.py acceptance run ...`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- `.workspace.local/` is local-only overlay state and must never be committed
- Never commit credentials, private hosts, private paths, tokens, or keys

## Workflow Routing

- Use `.agents/skills/workspace-init/SKILL.md` for staged workspace initialization
- Use `.agents/skills/workspace-foundation/SKILL.md` for local prerequisite readiness
- Use `.agents/skills/workspace-git-profile/SKILL.md` for repository identity and fork topology
- Use `.agents/skills/workspace-fleet/SKILL.md` for managed server lifecycle
- Use `benchmark run` or `acceptance run` for the public benchmark and real E2E happy path
- Use `.agents/skills/workspace-reset/SKILL.md` for explicit destructive teardown
- Use `.agents/skills/workspace-session-switch/SKILL.md` for session lifecycle intent
- Use `.agents/skills/workspace-sync/SKILL.md` for sync status and compatibility sync intent

## Adapter Boundary

- Treat shared `SKILL.md` files as the public contract layer
- Treat skill-local routing references as internal execution notes, not public workflow docs
