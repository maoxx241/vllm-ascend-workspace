# AGENTS

This repository is a public-facing control workspace for vLLM Ascend development workflows.

## Canonical constants

- Runtime root: `/vllm-workspace`
- Primary command surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup`, `./sync`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- Tracked submodules default to community `upstream` repositories.
- User-specific `origin` remotes are declared in `.workspace.local/repos.yaml`.
- For Codex bootstrap, the user should provide natural language input and the agent should perform the internal bootstrap steps.

## Commit hygiene

- `config/*.example.yaml` are the only tracked config templates for bootstrap guidance.
- `.workspace.local/` must stay local-only and is never committed.
- `.workspace.local/repos.yaml` is local state for repo topology and remote roles.
- Do not commit credentials, private hosts, private paths, tokens, or keys.
- `.agents/skills/` contains workspace-local reference material, not global skill installers.

## Environment assumptions

- Do not hardcode environment-specific hostnames or paths.
- Use placeholder/example values only in tracked files.
