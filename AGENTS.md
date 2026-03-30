# AGENTS

This repository is a public-facing control workspace for vLLM Ascend development workflows.

## Canonical constants

- Runtime root: `/vllm-workspace`
- Planned primary command surface: `tools/vaws.py`
- Planned compatibility entrypoints: `./setup`, `./sync`

## Commit hygiene

- `config/*.example.yaml` are the only tracked config templates for bootstrap guidance.
- `.workspace.local/` must stay local-only and is never committed.
- Do not commit credentials, private hosts, private paths, tokens, or keys.
- `docs/superpowers/` is local process documentation and is not part of the main public repo.

## Environment assumptions

- Do not hardcode environment-specific hostnames or paths.
- Use placeholder/example values only in tracked files.
