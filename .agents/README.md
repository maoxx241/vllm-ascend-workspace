# Repo-local skills

This directory contains the repository-local skill layer for Codex and other agent tooling.

## Layout

- `.agents/skills/repo-init/` is the source-of-truth skill package for repository initialization.
- `.claude/skills/repo-init/` is the Claude project-skill adapter.
- `AGENTS.md` carries repository-wide operating rules.
- `CLAUDE.md` carries Claude-specific project memory.

## Maintenance rule

If you change the behavior of `repo-init`, update both:

- `.agents/skills/repo-init/SKILL.md`
- `.claude/skills/repo-init/SKILL.md`

Keep the references and scripts under `.agents/skills/repo-init/` as the canonical supporting files.
