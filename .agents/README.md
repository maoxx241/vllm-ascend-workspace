# Repo-local skills

This directory contains the repository-local skill layer for Codex and other agent tooling.

## Layout

- `.agents/skills/repo-init/` is the source-of-truth skill package for repository initialization.
- `.agents/skills/machine-management/` is the source-of-truth skill package for remote machine attach, verify, repair, and removal workflows.
- `AGENTS.md` carries repository-wide operating rules.

## Maintenance rule

If you change the behavior of `repo-init`, update both:

- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/`
- `.agents/skills/repo-init/scripts/`

If you change the behavior of `machine-management`, update the repo-local skill package under:

- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/`
- `.agents/skills/machine-management/scripts/`

Keep the files under `.agents/skills/` as the canonical supporting files for repo-local skills.
