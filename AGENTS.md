# Repository instructions

This repository is a composable scaffold for local `vllm` + `vllm-ascend` development. It is not a mandatory workflow engine.

## Skills

- Repo-local skills live under `.agents/skills/`.
- `repo-init` is the only bundled skill in this package.
- `repo-init` is optional. Do not force it as a gate before ordinary exploration, coding, docs work, benchmarking, or serving tasks.
- Use `repo-init` when:
  - the user explicitly asks to initialize the repository after clone
  - the user asks to install or configure `gh`
  - the user asks to sign into GitHub
  - the user asks to initialize recursive submodules
  - the user asks to configure forks or remotes for the workspace, `vllm`, or `vllm-ascend`
  - missing GitHub auth, missing recursive submodules, or broken remote topology is the clear blocker for the requested work
- Do not use `repo-init` for unrelated Git operations, normal feature work, or runtime tasks.

## Operating rules for `repo-init`

- Stay idempotent and conservative.
- Ask before any environment-changing action:
  - installing tools
  - authenticating `gh`
  - generating or uploading SSH keys
  - forking repositories
  - renaming, deleting, or replacing remotes
  - syncing forks
  - moving branches
  - hard resetting branches
- Allow the user to decline any step without failing the whole run.
- Preserve extra remotes such as `upstream2`.
- Never store user-specific remotes, SSH private keys, personal access tokens, or host-specific secrets in tracked files.
- Prefer SSH for GitHub Git operations when possible.
- Support headless GitHub auth flows.
- If local privilege is missing, hand the user the prepared fallback script and command instead of trying to force a system install.

## Repository model

- `.gitmodules` must keep community URLs on `main`:
  - `https://github.com/vllm-project/vllm.git`
  - `https://github.com/vllm-project/vllm-ascend.git`
- Personal forks are local runtime state, not tracked state.
- `vllm-ascend` personal forks are recommended for PR work.
- `vllm` personal forks are optional.
- workspace personal forks are optional.
- The skill should optimize for the developer-visible repository state, not for preserving a detached submodule checkout forever.

## Validation

When you change the skill, review:
- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/acceptance.md`

Keep `.claude/skills/repo-init/SKILL.md` behavior-aligned with the Codex version.
