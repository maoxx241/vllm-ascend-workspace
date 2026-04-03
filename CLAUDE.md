# CLAUDE

This repository keeps its workflow contract inside the workspace.

## Always-On Context

- Canonical runtime root: `/vllm-workspace`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- `origin` and `upstream` repo topology is maintained as local workspace state
- Local workspace state must never be committed

## Public Skills

- Route first-time setup, recovery setup, or local foundation plus optional first-machine setup to `.agents/skills/workspace-init/SKILL.md`.
- Route machine attach, verify, and removal requests to `.agents/skills/machine-management/SKILL.md`.
- Route service lifecycle requests to `.agents/skills/serving/SKILL.md`.
- Route benchmark requests against an explicit ready service to `.agents/skills/benchmark/SKILL.md`.
- Route destructive teardown only to `.agents/skills/workspace-reset/SKILL.md`.

## Discovery

- Read the matched public `SKILL.md` first.
- When the skill leaves tool selection open, start at `.agents/discovery/README.md` before reading internal libraries.

## Adapter Boundary

- Treat shared `SKILL.md` files as the public contract layer.
- Treat skill-local routing references as internal execution notes only.
