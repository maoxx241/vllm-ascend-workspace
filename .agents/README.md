# Workspace-Local Agent Guidance

This directory contains the shared workspace-local contract layer for agents working in `vllm-ascend-workspace`.

- Canonical runtime root: `/vllm-workspace`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- Shared public contracts live under `.agents/skills/`
- `.workspace.local/` is local-only overlay state
- Local `origin` and `upstream` repository topology lives in `.workspace.local/repos.yaml`

## Public Skills

- `.agents/skills/workspace-init/` prepares the repo for development.
- `.agents/skills/machine-management/` handles machine attach, verify, and removal requests.
- `.agents/skills/benchmark/` handles benchmark execution requests.
- `.agents/skills/workspace-reset/` handles explicit destructive teardown.
- `.agents/skills/profiling-analysis/` remains available as a domain-specific skill.

## Boundary

- Shared `SKILL.md` files are the public contract layer.
- Skill-local routing references stay internal.
- Keep private tokens, private hosts, and private path references out of tracked files.
