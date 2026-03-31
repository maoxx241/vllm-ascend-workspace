# AGENTS

This repository is a public-facing control workspace for vLLM Ascend development workflows.

## Canonical constants

- Runtime root: `/vllm-workspace`
- Primary command surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup`, `./sync`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`

## Commit hygiene

- `config/*.example.yaml` are the only tracked config templates for bootstrap guidance.
- `.workspace.local/` must stay local-only and is never committed.
- Do not commit credentials, private hosts, private paths, tokens, or keys.
- `.agents/skills/` contains workspace-local reference material, not global skill installers.

## Environment assumptions

- Do not hardcode environment-specific hostnames or paths.
- Use placeholder/example values only in tracked files.
