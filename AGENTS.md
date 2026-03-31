# AGENTS

This repository is a public-facing control workspace for vLLM Ascend development workflows.

## Always-On Context

- Runtime root: `/vllm-workspace`
- Primary command surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup`, `./sync`
- Source repos: `vllm/`, `vllm-ascend/` as submodules
- Clone and refresh sources recursively: `git submodule update --init --recursive`
- `config/*.example.yaml` are the only tracked config templates for bootstrap guidance.
- `.workspace.local/` must stay local-only and is never committed.
- Do not commit credentials, private hosts, private paths, tokens, or keys.

## Workflow Routing

- Keep detailed operational procedures in `.agents/skills/`, not in this file.
- Use `.agents/skills/workspace-bootstrap/SKILL.md` for bootstrap and bootstrap repair.
- Use `.agents/skills/workspace-reset/SKILL.md` for guarded reset / deinit.
- Use `.agents/skills/workspace-session-switch/SKILL.md` for session changes.
- Use `.agents/skills/workspace-sync/SKILL.md` for sync behavior.
