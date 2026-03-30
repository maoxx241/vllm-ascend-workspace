# Workspace-Local Agent Guidance

This directory contains public, workspace-local guidance for agents working in `vllm-ascend-workspace`.

- Canonical runtime root: `/vllm-workspace`
- Primary command surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup` and `./sync`
- Local overlay state: `.workspace.local/`
- These files are guidance only.
- Keep private tokens, private hosts, and legacy canonical path references out of tracked files.
