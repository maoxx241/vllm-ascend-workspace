# AGENTS

This repository is a public-facing control workspace for vLLM Ascend development workflows.

## Always-On Context

- Runtime root: `/vllm-workspace`
- Primary implementation surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup`, `./sync`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- `.workspace.local/` is local-only overlay state and must never be committed
- Never commit credentials, private hosts, private paths, tokens, or keys

## Workflow Routing

- Use `.agents/skills/workspace-bootstrap/SKILL.md` for first baseline bootstrap intent
- Use `.agents/skills/workspace-fleet/SKILL.md` for post-bootstrap server management
- Use `.agents/skills/workspace-reset/SKILL.md` for explicit destructive teardown
- Use `.agents/skills/workspace-session-switch/SKILL.md` for session lifecycle intent
- Use `.agents/skills/workspace-sync/SKILL.md` for sync status and compatibility sync intent

## Adapter Boundary

- Treat shared `SKILL.md` files as the public contract layer
- Treat skill-local routing references as internal execution notes, not public workflow docs
