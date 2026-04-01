# AGENTS

This repository is an agent-first scaffold for vLLM Ascend development work.

## Always-On Context

- Canonical runtime root: `/vllm-workspace`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- `origin` and `upstream` repo topology is tracked through `.workspace.local/repos.yaml`
- `.workspace.local/` is local-only overlay state and must never be committed
- Never commit credentials, private hosts, private paths, tokens, or keys

## Public Skills

- Use `.agents/skills/workspace-init/SKILL.md` for first-time setup or recovery setup.
- Use `.agents/skills/machine-management/SKILL.md` to attach, verify, or remove machines.
- Use `.agents/skills/benchmark/SKILL.md` to run benchmark workflows on a ready environment.
- Use `.agents/skills/workspace-reset/SKILL.md` only for explicit destructive teardown.

## Skill Boundary

- Treat shared `SKILL.md` files as the public contract layer.
- Treat skill-local routing references as internal execution notes, not public workflow docs.
