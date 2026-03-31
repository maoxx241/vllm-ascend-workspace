---
name: workspace-fleet
description: Use when managing workspace servers after bootstrap, especially for add/list/verify/repair-style maintenance against `.workspace.local/servers.yaml`.
---

# Workspace Fleet

Use this skill for post-bootstrap server inventory maintenance.

- Treat `.workspace.local/servers.yaml` as the source of truth for managed servers.
- Use `tools/vaws.py fleet list` to inspect the current inventory.
- Use `tools/vaws.py fleet add` to add or realize a managed server.
- Use `tools/vaws.py fleet verify` to check a server without mutating local inventory.
- Treat verification gaps as repair signals, not bootstrap failures.
- Keep server-specific changes out of tracked files; they belong in the local overlay.
- Use bootstrap only for first-time baseline setup, not for later server changes.

This is workspace-local reference material only.
