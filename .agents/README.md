# Repo-local skills

This directory contains the repository-local skill layer for Codex, Claude Code, and similar agents.

## Layout

- `.agents/skills/repo-init/` is the source-of-truth skill package for repository initialization.
- `.agents/skills/machine-management/` is the source-of-truth skill package for remote machine attach, verify, repair, and removal workflows.
- `AGENTS.md` carries repository-wide routing and operating rules.

## Script-first convention

When a workflow has deterministic shell, SSH, or Git mechanics, prefer the helper under `scripts/` instead of rebuilding the command inline in the conversation.

Current primary helpers:

- `repo-init/scripts/repo_init_probe.py`
- `repo-init/scripts/repo_topology.py`
- `machine-management/scripts/inventory.py`
- `machine-management/scripts/manage_machine.py`

Reference files under `references/` are fallback detail, not the default execution path.

## Maintenance rule

If you change `repo-init`, update these together:

- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/`
- `.agents/skills/repo-init/scripts/`

If you change `machine-management`, update these together:

- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/`
- `.agents/skills/machine-management/scripts/`

Keep the files under `.agents/skills/` as the canonical supporting files for repo-local skills.
