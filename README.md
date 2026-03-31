# vllm-ascend-workspace

Public workspace-local control repository for coordinating vLLM and vLLM-Ascend work.

## Entry Points

- `tools/vaws.py` is the primary CLI.
- `./setup` is the bootstrap compatibility entrypoint.
- `./sync` is the compatibility sync entrypoint.

## Runtime Root

- Canonical container/runtime path: `/vllm-workspace`

## Source Repos

- `vllm/` is tracked as a workspace submodule.
- `vllm-ascend/` is tracked as a workspace submodule.
- Tracked submodule defaults point at community `upstream` repositories.
- User-owned `origin` remotes belong in `.workspace.local/repos.yaml`, not in tracked files.
- Bootstrap the sources with `git clone --recursive` or `git submodule update --init --recursive`.
- Recursive init matters because `vllm-ascend/` also carries nested submodules.
- The control repo lives at `/vllm-workspace/workspace`.

## Local Overlay

- `.workspace.local/` is local-only overlay state and stays untracked.
- `.workspace.local/repos.yaml` is the local source of truth for `origin`/`upstream` repo topology.
- Tracked files must not contain private tokens, private hosts, or legacy path references.

## Agent Workflows

- `.agents/skills/` contains public reference material for workspace-local agents.
- Bootstrap and first-time baseline flow live in `.agents/skills/workspace-bootstrap/`.
- Ongoing server inventory maintenance lives in `.agents/skills/workspace-fleet/`.
- For Codex bootstrap, the user should start from a natural language request and the agent should route into the bootstrap skill.
- Guarded best-effort cleanup flow lives in `.agents/skills/workspace-reset/`.
- Session switching flow lives in `.agents/skills/workspace-session-switch/`.
- Sync flow lives in `.agents/skills/workspace-sync/`.
