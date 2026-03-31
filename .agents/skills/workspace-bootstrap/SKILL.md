---
name: workspace-bootstrap
description: Public workspace-local guidance for bootstrap and repair flows.
---

# Workspace Bootstrap

Use this skill when initializing or repairing the local workspace overlay.

- Keep the canonical runtime root at `/vllm-workspace`.
- Use `tools/vaws.py init` and `tools/vaws.py doctor` for bootstrap checks.
- Treat `.workspace.local/` as local overlay state only.
- Prefer `config/*.example.yaml` as the public template source.
- Keep secrets, private hosts, and private paths out of tracked files.

This is workspace-local reference material only.
