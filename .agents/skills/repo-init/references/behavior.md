# Repo-init behavior reference

This file is the detailed contract for the `repo-init` skill.

## Core contract

`repo-init` prepares a cloned workspace for development. It is optional, repeatable, and conservative.

The skill must never:

- write personal remotes into tracked files
- write credentials into tracked files
- force repository initialization before unrelated work
- silently remove extra remotes
- silently reset user branches

## Stage model

### Stage 0: applicability

Confirm that the current repository is the workspace control repo or that the user explicitly wants to apply the same initialization pattern here.

### Stage 1: state probe

Preferred command:

```bash
python3 .agents/skills/repo-init/scripts/repo_init_probe.py
```

The probe is read-only. Use it first whenever possible.

### Stage 2: mutation boundaries

Ask before each mutation category:

| Category | Ask first |
| --- | --- |
| install `gh` | yes |
| install Homebrew | yes |
| authenticate GitHub | yes |
| create or upload SSH keys | yes |
| initialize recursive submodules | yes |
| fork repos | yes |
| rename or replace remotes | yes |
| sync forks | yes |
| move branches | yes |
| hard reset any branch | yes |

### Stage 3: GitHub CLI installation

Preferred strategies by platform:

| Platform | Preferred strategy |
| --- | --- |
| macOS | Homebrew |
| Ubuntu | official GitHub CLI Debian repo |
| WSL | official GitHub CLI Debian repo inside the Linux environment |
| Windows | `winget` |

If the machine lacks the needed privilege, do not abandon the run. Instead:

- explain the limitation
- hand the user the prepared fallback command
- continue with the rest of the read-only diagnosis if possible

### Stage 4: auth

Preferred auth order:

1. `github.com`
2. SSH git protocol
3. `gh auth login` web/device flow
4. headless token flow when browser/device flow is not workable

The skill should use `gh` because the same CLI is useful later for:
- forking
- sync
- PR creation
- default repo selection

### Stage 5: recursive submodules

Always do both:

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

`vllm-ascend` may have nested submodules. This repository should never be considered fully initialized after only a non-recursive submodule update.

## Remote decision table

### workspace

Expected community upstream:

- `maoxx241/vllm-ascend-workspace`

Rules:

- If the user clone is already their fork, offer to add community `upstream`.
- If the user has a fork and is currently on the community repo, offer to switch `origin` to the user's fork.
- If there is no user fork, leaving the current remote layout unchanged is valid.

### `vllm`

Expected community upstream:

- `vllm-project/vllm`

Rules:

- user fork is optional
- if a user fork exists, offer to use it as `origin`
- if no user fork exists, community-only mode is valid
- if the user fork lags the community default branch, ask before syncing it

### `vllm-ascend`

Expected community upstream:

- `vllm-project/vllm-ascend`

Rules:

- user fork is recommended
- if no user fork exists, recommend creating one
- if the user declines, preserve community-only mode
- if the user fork lags the community default branch, ask before syncing it

## Branch-placement rules

`repo-init` should optimize for a development-friendly local state:

- `workspace`: usually keep the current branch unless the user wants something else
- `vllm`: prefer local `main` tracking the working remote's `main`
- `vllm-ascend`: prefer local `main` tracking the working remote's `main`

Safety rules:

- if the worktree is dirty, ask before moving branches
- if the branch does not exist locally, create it from the approved remote
- prefer `git pull --ff-only`
- do not use `git reset --hard` unless the user explicitly approves it

## `gh` default repo

When both `origin` and `upstream` exist, use `gh repo set-default upstream` unless the user asks for another default. This matches the usual “push branch to fork, PR to upstream” flow.

## Community-only mode

Community-only mode is a valid end state.

It means:

- no personal fork is required
- `origin` may remain pointed at the community repo
- `upstream` may be absent
- the skill still succeeds, but should report that PR-oriented fork workflows are limited

## Extra remotes

Extra remotes such as `upstream2`, `partner`, or `mirror` are normal.

The skill should:

- preserve them
- mention them in summaries when relevant
- never remove them unless the user explicitly asks

## No-admin fallback

If `gh` installation cannot be completed with system privilege:

- POSIX: hand off `.agents/skills/repo-init/scripts/install_gh_user.py`
- Windows: hand off `.agents/skills/repo-init/scripts/install-gh-user.ps1`

The skill should explicitly say the handoff happened and should not claim the machine is already fully prepared until the user has run the fallback installer.
