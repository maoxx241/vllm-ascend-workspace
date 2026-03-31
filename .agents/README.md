# Workspace-Local Agent Guidance

This directory contains public, workspace-local reference material for agents working in `vllm-ascend-workspace`.

- Canonical runtime root: `/vllm-workspace`
- Primary command surface: `tools/vaws.py`
- Compatibility entrypoints: `./setup` and `./sync`
- Local overlay state: `.workspace.local/`
- Current control-plane session manifests: `.workspace.local/sessions/<session>/manifest.yaml`
- Target runtime session manifests: `/vllm-workspace/.vaws/sessions/<session>/manifest.yaml`
- These files are reference material only.
- Keep private tokens, private hosts, and legacy path references out of tracked files.
