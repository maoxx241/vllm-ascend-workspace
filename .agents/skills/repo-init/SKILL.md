---
name: repo-init
description: Initialize this workspace after clone. Use when the user asks to set up GitHub CLI/auth, recursive submodules, and the recommended fork/remote topology for the workspace, vllm, or vllm-ascend. Do not use for ordinary coding, serving, benchmarking, or unrelated Git tasks.
---

# Repo Init

Use this skill to prepare a fresh or drifted `vllm-ascend-workspace` clone for development. This skill is optional and should never be treated as a hard prerequisite for unrelated work.

## Outcome

A successful run leaves the local clone in a recommended but editable state:

- `gh` is installed, or the user has been given a prepared no-admin fallback command.
- GitHub auth exists on `github.com`.
- SSH is preferred for Git operations when it is usable.
- submodules are initialized recursively.
- local remotes and local branches reflect the user's choices.
- community URLs remain in tracked files; user-specific topology stays local.

## Principles

- Be **idempotent**.
- Be **conservative**.
- Keep the flow **natural-language driven**.
- Ask before any environment-changing action.
- Allow the user to decline any stage without aborting the whole run.
- Preserve extra remotes such as `upstream2`.
- Never write secrets or user-specific remotes into tracked files.
- Do not force `repo-init` as a gate before later skills or normal coding.

## When to use this skill

Use it when the user asks to:

- initialize the workspace after clone
- install or configure GitHub CLI
- sign into GitHub
- initialize submodules recursively
- configure forks or remotes for the workspace, `vllm`, or `vllm-ascend`

You may also use it when missing GitHub auth, missing recursive submodules, or obviously broken remote topology is the clear blocker for the requested task.

## When not to use this skill

Do not use it for:

- normal code changes
- bug fixes
- docs-only work
- serving or benchmarking tasks
- generic Git work that is unrelated to initial repository setup

## Supporting files

Open these only when you need more detail:

- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/command-recipes.md`
- `.agents/skills/repo-init/references/acceptance.md`

Probe the machine and repository state with:

- POSIX: `python3 .agents/skills/repo-init/scripts/repo_init_probe.py`
- Windows: `py -3 .agents/skills/repo-init/scripts/repo_init_probe.py`

## Canonical topology

Treat the following as the recommended target state, not a mandatory one.

| Repository | Recommended `origin` | Recommended `upstream` | Notes |
| --- | --- | --- | --- |
| workspace | user fork, if the user wants one | `maoxx241/vllm-ascend-workspace` | If current clone is already the user fork, offer to add `upstream`. |
| `vllm` | user fork, if one exists and the user wants it | `vllm-project/vllm` | Community-only mode is valid. |
| `vllm-ascend` | user fork | `vllm-project/vllm-ascend` | Personal fork is recommended for PR-oriented work. |

## Execution workflow

Follow the stages in order. Skip or stop at any stage the user declines.

### 1. Probe first

Start with the probe script. It reports:

- OS and package managers
- `git`, `gh`, `ssh`, `ssh-keygen`, `brew`, `apt`, and `winget` availability
- current GitHub auth state
- current remote topology for workspace, `vllm`, and `vllm-ascend`
- recursive submodule status
- whether the logged-in user appears to have matching personal forks

If the probe script cannot run, fall back to direct shell commands.

### 2. Summarize before acting

Before making changes, summarize the current state briefly:

- whether `gh` exists
- whether GitHub auth exists
- whether SSH is ready
- whether submodules are initialized
- which forks exist
- what each repo currently uses for `origin` and `upstream`

Then ask for the next category of changes.

### 3. Ensure `gh`

Preferred install choices:

- macOS: Homebrew. If Homebrew is missing, offer to install Homebrew first.
- Ubuntu / WSL: the official GitHub CLI Debian package source.
- Windows: `winget`.
- no sudo / no admin: hand the user the prepared fallback installer script instead of forcing a system install.

Use the fallback scripts in this skill when local privilege is missing:

- POSIX fallback: `python3 .agents/skills/repo-init/scripts/install_gh_user.py`
- Windows fallback: `powershell -ExecutionPolicy Bypass -File .agents/skills/repo-init/scripts/install-gh-user.ps1`

Do not silently modify user shell startup files or PATH settings without saying so.

### 4. Ensure GitHub auth

Preferred order:

1. `github.com`
2. SSH for Git operations
3. web/device flow when available
4. headless token flow when browser access is unavailable

Use `gh` for the login flow so later PR workflows stay aligned.

Preferred interactive login:

```bash
gh auth login --hostname github.com --git-protocol ssh --web
```

For headless use, prefer an environment token or `--with-token`. After token-based login, still try to align Git operations to SSH when SSH is usable.

After login, verify with:

```bash
gh auth status --hostname github.com
gh api user --jq .login
```

If SSH is requested and no key exists, ask before creating or uploading one. Prefer letting `gh auth login --git-protocol ssh` handle key detection and creation. If a manual path is needed, use `ssh-keygen` plus `gh ssh-key add`.

### 5. Initialize submodules recursively

Always use recursive sync + init for this repo:

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

`vllm-ascend` may carry nested submodules, so non-recursive init is insufficient.

### 6. Decide each repository independently

#### workspace

- If current clone is already the user's fork, offer to add `maoxx241/vllm-ascend-workspace` as `upstream`.
- If the user has a workspace fork but current `origin` is not that fork, offer to switch `origin` to the user's fork and keep `maoxx241/vllm-ascend-workspace` as `upstream`.
- If the user has no workspace fork, leaving the current topology alone is valid.

#### `vllm`

- If the user has a personal fork, offer to use it as `origin` and use `vllm-project/vllm` as `upstream`.
- If the user does not have a personal fork, community-only mode is valid; keep the community repo as the working remote.
- If the user fork exists but looks behind the community `main`, ask before syncing it.

#### `vllm-ascend`

- If the user does not have a fork, recommend creating one.
- If the user agrees, create or adopt the fork and wire `origin` -> user fork, `upstream` -> `vllm-project/vllm-ascend`.
- If the user refuses, stay in community-only mode and clearly say that PR-oriented flows will be more limited.
- If the user fork exists but looks behind the community `main`, ask before syncing it.

### 7. Sync only with approval

When a user fork exists but is behind the community default branch, ask before syncing it.

Preferred sync command pattern:

```bash
gh repo sync USER/REPO --source OWNER/REPO
```

Use:

- `gh repo sync USER/vllm --source vllm-project/vllm`
- `gh repo sync USER/vllm-ascend --source vllm-project/vllm-ascend`
- `gh repo sync USER/vllm-ascend-workspace --source maoxx241/vllm-ascend-workspace`

### 8. Move local branches only when safe

The developer-visible state matters more than preserving a detached submodule checkout forever, but branch movement is still a local mutation.

Rules:

- Prefer a local `main` tracking the chosen working remote's `main` for `vllm` and `vllm-ascend`.
- Prefer fast-forward updates.
- If the worktree is dirty, ask before changing branches or pulling.
- Do not hard reset without explicit approval.

Good patterns:

```bash
git fetch origin --prune
git switch main || git switch -c main --track origin/main
git branch --set-upstream-to=origin/main main
git pull --ff-only
```

If the user is in community-only mode, replace `origin` with the community remote actually being used.

### 9. Set `gh` default repo for PR-oriented flows

When both `origin` and `upstream` exist, prefer setting the `gh` default repo to `upstream` unless the user asks otherwise. This matches the common “develop on my fork, open PR to community” workflow.

Example:

```bash
gh repo set-default upstream
```

### 10. Finish with an explicit status report

End with a compact report that covers:

- tool install state
- auth state
- submodule state
- remote topology for each repo
- branch placement for each repo
- skipped or declined steps
- any remaining manual action for the user

## Important edge cases

- Custom remotes are allowed. Do not delete them just because they are nonstandard.
- If any repository has local changes, avoid aggressive normalization.
- If the user refuses a fork, community-only mode is still a successful outcome.
- If the user refuses sync, keep the fork as-is and say so.
- If the user refuses branch movement, keep the current branch state and say so.
- If privilege is missing, hand off the prepared fallback installer rather than pretending the install succeeded.
