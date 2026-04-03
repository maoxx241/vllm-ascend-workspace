# Workspace-Local Agent Guidance

This directory contains the shared workspace-local contract layer for agents working in `vllm-ascend-workspace`.

- Canonical runtime root: `/vllm-workspace`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- Shared public contracts live under `.agents/skills/`
- Local overlay state stays untracked
- Local `origin` and `upstream` repository topology stays in workspace-local state

## Public Skills

- `.agents/skills/workspace-init/` prepares the local repo foundation for development and can extend that baseline to an optional first machine.
- `.agents/skills/machine-management/` handles machine attach, verify, and removal requests.
- `.agents/skills/serving/` handles model-serving start, status, list, and stop requests.
- `.agents/skills/benchmark/` handles benchmark execution requests against an explicit ready model service.
- `.agents/skills/workspace-reset/` handles explicit destructive teardown.
- `.agents/skills/profiling-analysis/` remains available as a domain-specific skill.

## Discovery

- Read the matched public `SKILL.md` first.
- Open `.agents/discovery/README.md` only when the matched skill or its linked reference does not identify the next tool, or when probe results disagree and the next step is ambiguous.

## Boundary

- Shared `SKILL.md` files are the public contract layer.
- Skill-local routing references stay internal.
- Keep private tokens, private hosts, and private path references out of tracked files.
