# Repo-init acceptance criteria

This file is for validating the `repo-init` skill.

## Trigger examples

These should trigger `repo-init`:

- “Initialize this workspace.”
- “Set up GitHub CLI and sign me in.”
- “Prepare submodules and forks for vLLM Ascend PR work.”
- “Configure my fork remotes for `vllm-ascend`.”
- “I just cloned the repo. Set me up.”

## Non-trigger examples

These should not trigger `repo-init` unless the missing setup is the obvious blocker:

- “Fix this failing unit test.”
- “Explain how `vllm-ascend` scheduling works.”
- “Run the benchmark suite.”
- “Update README wording.”
- “Review this diff.”

## Success criteria

A successful run should satisfy all applicable items below.

### Universal

- The skill probes first before making changes.
- The skill asks before every environment-changing action.
- The skill allows partial completion when the user declines a step.
- The skill never writes personal remotes or credentials into tracked files.
- The skill preserves nonstandard extra remotes.

### Tooling

- If `gh` is missing, the skill selects the correct platform-specific install path.
- If privilege is unavailable, the skill hands off the prepared fallback installer instead of pretending installation succeeded.

### Auth

- The skill verifies login with `gh auth status` and `gh api user --jq .login`.
- SSH is preferred when feasible.
- Headless environments are supported with token-based login flows.

### Submodules

- The skill uses recursive submodule sync + init.
- The skill recognizes that `vllm-ascend` may have nested submodules.

### Remotes

- workspace:
  - if already on the user's fork, the skill offers to add community `upstream`
  - if a user fork exists but is not configured, the skill offers to adopt it
- `vllm`:
  - user fork use is optional
  - community-only mode is valid
- `vllm-ascend`:
  - a user fork is recommended
  - community-only mode is allowed when the user declines

### Branches

- The skill prefers a developer-friendly `main` branch state for `vllm` and `vllm-ascend`.
- The skill asks before moving branches in dirty repositories.
- The skill does not hard reset without explicit approval.

## Scenario matrix

| Scenario | Expected result |
| --- | --- |
| fresh clone, no `gh`, browser available | install `gh`, log in with `gh`, init recursive submodules, then walk repo topology decisions |
| fresh clone, no `gh`, headless, no sudo | hand off fallback installer, support token-based auth path, still provide a partial setup report |
| workspace already cloned from the user's fork | offer to add `maoxx241/vllm-ascend-workspace` as `upstream` |
| user has a `vllm` fork but declines switching `origin` | keep current `origin`, report that the fork was available but intentionally not adopted |
| user has no `vllm-ascend` fork and agrees to create one | create or adopt fork, wire `origin` and `upstream`, optionally sync and place local `main` |
| user has no `vllm-ascend` fork and declines | stay in community-only mode, report that PR-oriented flow is limited |
| custom `upstream2` remote already exists | preserve it |
| `vllm` or `vllm-ascend` worktree is dirty | ask before changing branch or syncing |

## Manual regression checklist

Review these files together after every substantial skill edit:

- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/command-recipes.md`
- `.claude/skills/repo-init/SKILL.md`
