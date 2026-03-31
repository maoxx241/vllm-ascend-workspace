---
name: workspace-sync
description: Public workspace-local guidance for repository and session sync flows.
---

# Workspace Sync

Use this skill when synchronizing repo state or compatibility wrappers.

- Keep `tools/vaws.py sync` as the public sync entrypoint.
- Preserve `./sync` as the compatibility wrapper.
- Default new workspace references to `origin/main` unless local overlay data says otherwise.
- Never develop or push directly on `main`; branch to `feature/*` first.
- Land tracked changes on `main` only through a pull request.
- Squash the PR branch commits before merge so one PR becomes one commit on `main`.
- Keep tracked files free of private hosts, tokens, and old canonical paths.
- Use example configs and local overlay files for environment-specific values.

This is workspace-local reference material only.
