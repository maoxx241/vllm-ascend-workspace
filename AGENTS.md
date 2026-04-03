# AGENTS

This repository is an agent-first scaffold for vLLM Ascend development work.

## Always-On Context

- Canonical runtime root: `/vllm-workspace`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- `origin` and `upstream` repo topology is maintained as local workspace state
- Local workspace state must never be committed
- Never commit credentials, private hosts, private paths, tokens, or keys

## Public Skills

- Use `.agents/skills/workspace-init/SKILL.md` for first-time setup, recovery setup, or local foundation plus optional first-machine setup.
- Use `.agents/skills/machine-management/SKILL.md` to attach, verify, or remove machines.
- Use `.agents/skills/serving/SKILL.md` to start, inspect, list, or stop model services on a ready machine.
- Use `.agents/skills/benchmark/SKILL.md` to run benchmark workflows against an explicit ready model service.
- Use `.agents/skills/workspace-reset/SKILL.md` only for explicit destructive teardown.

## Discovery

- Read the matched public `SKILL.md` first.
- Open `.agents/discovery/README.md` only when the matched skill or its linked reference does not identify the next tool, or when probe results disagree and the next step is ambiguous.

## Skill Boundary

- Treat shared `SKILL.md` files as the public contract layer.
- Treat skill-local routing references as internal execution notes, not public workflow docs.
