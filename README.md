# vllm-ascend-workspace

Public workspace-local scaffold for coordinating vLLM and vLLM-Ascend development with agent-driven workflows.

## Runtime Root

- Canonical container and runtime path: `/vllm-workspace`

## Source Repos

- `vllm/` is tracked as a workspace submodule.
- `vllm-ascend/` is tracked as a workspace submodule.
- Tracked submodule defaults point at community `upstream` repositories.
- User-owned `origin` remotes live in local workspace state, not in tracked files.
- Clone and refresh sources recursively with `git submodule update --init --recursive`.
- Recursive submodule checkout matters because `vllm-ascend/` also carries nested submodules.
- The control repo lives at `/vllm-workspace/workspace`.

## Local Overlay

- Local overlay state stays untracked.
- `origin` and `upstream` repo topology comes from local workspace state, not tracked files.
- Tracked files must not contain private tokens, private hosts, or private path references.

## Public Skills

- `.agents/skills/workspace-init/` prepares the repo for development, including Git setup and optional first machine setup.
- `.agents/skills/machine-management/` attaches, verifies, and removes machines.
- `.agents/skills/benchmark/` runs benchmark workflows on a ready environment.
- `.agents/skills/workspace-reset/` handles explicit destructive teardown.
- `.agents/skills/profiling-analysis/` remains available as an orthogonal domain skill.

## Adapters

- `AGENTS.md` is the Codex adapter.
- `CLAUDE.md` is the Claude Code adapter.
- `.cursorrules` is the Cursor adapter.
- `.agents/README.md` explains the shared workspace-local skill layer.
