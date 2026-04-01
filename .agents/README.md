# Workspace-Local Agent Guidance

This directory contains the shared workspace-local contract layer for agents working in `vllm-ascend-workspace`.

- Canonical runtime root: `/vllm-workspace`
- Primary implementation surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup` and `./sync`
- Shared public contracts live under `.agents/skills/`
- Exact internal routing notes live under each skill’s `references/` directory
- First baseline contract: `.agents/skills/workspace-bootstrap/`
- Post-bootstrap server management contract: `.agents/skills/workspace-fleet/`
- Destructive teardown contract: `.agents/skills/workspace-reset/`
- Session lifecycle contract: `.agents/skills/workspace-session-switch/`
- Session-oriented compatibility sync contract: `.agents/skills/workspace-sync/`
- `.workspace.local/` is local-only overlay state
- Local `origin` / `upstream` repository topology lives in `.workspace.local/repos.yaml`
- Bootstrap starts from natural language user intent and resolves through shared contracts instead of direct CLI phrasebooks
- Keep private tokens, private hosts, and legacy path references out of tracked files
