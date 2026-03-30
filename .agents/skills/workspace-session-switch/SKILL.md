---
name: workspace-session-switch
description: Public workspace-local guidance for switching feature sessions.
---

# Workspace Session Switch

Use this skill when changing the active feature session in `.workspace.local/state.json`.

- Keep session manifests under `/vllm-workspace/.vaws/sessions/<session>/manifest.yaml`.
- Preserve `/vllm-workspace` as the runtime root inside manifests and wrappers.
- Route session changes through `tools/vaws.py session switch`.
- Reject unsafe session names and avoid writing private environment details into tracked files.
- Keep session guidance local to this repository.

This is workspace-local reference material only.
