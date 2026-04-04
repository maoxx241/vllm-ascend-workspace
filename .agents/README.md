# Repo-local skills

This directory contains the repository-local skill layer for Codex, Claude Code, and similar agents.

## Layout

- `.agents/skills/repo-init/` is the source-of-truth skill package for repository initialization.
- `.agents/skills/machine-management/` is the source-of-truth skill package for remote machine attach, verify, repair, and removal workflows.
- `.agents/scripts/workspace_profile.py` is the shared helper for the local workspace machine profile.
- `.agents/lib/vaws_local_state.py` is the shared library for untracked local runtime state.
- `AGENTS.md` carries repository-wide routing rules and mandatory decision gates.

## Script-first convention

When a workflow has deterministic shell, SSH, Git, or local-state mechanics, prefer the helper script instead of rebuilding the command inline in the conversation.

Current primary helpers:

- `repo-init/scripts/repo_init_probe.py`
- `repo-init/scripts/repo_topology.py`
- `machine-management/scripts/inventory.py`
- `machine-management/scripts/manage_machine.py`
- `../scripts/workspace_profile.py`

Reference files under `references/` are fallback detail, not the default execution path.

## Local runtime state

Untracked workspace-local state lives under `.vaws-local/`:

- `.vaws-local/machine-profile.json`
- `.vaws-local/machine-inventory.json`

The legacy repo-root `.machine-inventory.json` is compatibility input only and should not be reintroduced as the primary path.

Key guardrail:

- on a missing machine profile, `workspace_profile.py ensure` now requires either `--username` or `--generate`
- this prevents silent default usernames during broad init or first machine attach

## Maintenance rule

If you change `repo-init`, update these together:

- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/`
- `.agents/skills/repo-init/scripts/`
- shared helpers when the workflow depends on local profile state

If you change `machine-management`, update these together:

- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/`
- `.agents/skills/machine-management/scripts/`
- shared helpers when the workflow depends on local profile or inventory state

Keep the files under `.agents/skills/` as the canonical supporting files for repo-local skills.
