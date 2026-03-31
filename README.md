# vllm-ascend-workspace

Public workspace-local control repository for coordinating vLLM and vLLM-Ascend work.

## Current entrypoints

- `tools/vaws.py` is the primary CLI.
- `./setup` is the bootstrap compatibility entrypoint.
- `./sync` is the compatibility sync entrypoint.

## Runtime root

- Canonical container/runtime path: `/vllm-workspace`

## Source repos

- `vllm/` is tracked as a workspace submodule.
- `vllm-ascend/` is tracked as a workspace submodule.
- The control repo lives at `/vllm-workspace/workspace`.

## Workspace-local reference

- `.agents/skills/` contains public reference material for workspace-local agents.
- `.workspace.local/` is local-only overlay state and stays untracked.
- Tracked files must not contain private tokens, private hosts, or legacy path references.
