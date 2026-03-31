# vllm-ascend-workspace

Public workspace-local control repository for coordinating vLLM and vLLM-Ascend work.

## Current entrypoints

- `tools/vaws.py` is the primary CLI.
- `./setup` is the bootstrap compatibility entrypoint.
- `./sync` is the compatibility sync entrypoint.
- For Codex users, bootstrap should start from natural language requests and the agent should drive the internal command flow.

## Runtime root

- Canonical container/runtime path: `/vllm-workspace`

## Source repos

- `vllm/` is tracked as a workspace submodule.
- `vllm-ascend/` is tracked as a workspace submodule.
- Tracked submodule defaults point at community `upstream` repositories.
- User-owned `origin` remotes belong in `.workspace.local/repos.yaml`, not in tracked files.
- Bootstrap the sources with `git clone --recursive` or `git submodule update --init --recursive`.
- Recursive init matters because `vllm-ascend/` also carries nested submodules.
- The control repo lives at `/vllm-workspace/workspace`.

## Workspace-local reference

- `.agents/skills/` contains public reference material for workspace-local agents.
- `.workspace.local/` is local-only overlay state and stays untracked.
- `.workspace.local/repos.yaml` is the local source of truth for `origin`/`upstream` repo topology.
- Tracked files must not contain private tokens, private hosts, or legacy path references.
