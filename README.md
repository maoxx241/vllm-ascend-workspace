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
- Bootstrap starts from natural language workspace requests and resolves them against the tracked `upstream` defaults plus local `origin` overlay data.

## Agent Workflows

- `.agents/skills/` contains the shared workspace-local contracts for bootstrap, fleet, reset, session-switch, and sync.
- `AGENTS.md` is the Codex adapter.
- `CLAUDE.md` is the Claude Code adapter.
- `.cursorrules` is the Cursor adapter.
- Exact internal routing is kept in skill-local `references/internal-routing.md` files and is not the public workflow surface.
- Bootstrap and first baseline flow live in `.agents/skills/workspace-bootstrap/`.
- Ongoing server inventory management lives in `.agents/skills/workspace-fleet/`.
- Guarded best-effort cleanup flow lives in `.agents/skills/workspace-reset/`.
- Session lifecycle flow lives in `.agents/skills/workspace-session-switch/`.
- Session-oriented compatibility sync flow lives in `.agents/skills/workspace-sync/`.
