# Workspace-Local Agent Guidance

This directory contains public, workspace-local reference material for agents working in `vllm-ascend-workspace`.

- Canonical runtime root: `/vllm-workspace`
- Primary command surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup` and `./sync`
- Workflow skills live under `.agents/skills/`.
- Bootstrap and repair procedure: `.agents/skills/workspace-bootstrap/`
- For Codex bootstrap, start from natural language user input and route into the bootstrap skill.
- Guarded reset and deinit procedure: `.agents/skills/workspace-reset/`
- Session switching procedure: `.agents/skills/workspace-session-switch/`
- Sync procedure: `.agents/skills/workspace-sync/`
- Local overlay state: `.workspace.local/`
- `.workspace.local/repos.yaml` stores local `origin`/`upstream` repo topology.
- Tracked repo defaults stay on community `upstream` repositories; user forks are configured locally.
- Current control-plane session manifests: `.workspace.local/sessions/<session>/manifest.yaml`
- Target runtime session manifests: `/vllm-workspace/.vaws/sessions/<session>/manifest.yaml`
- These files are reference material only.
- Keep private tokens, private hosts, and legacy path references out of tracked files.
