---
name: repo-init
description: Initialize this workspace after clone. Use for requests like “初始化仓库”, “配置 gh / GitHub 登录”, “初始化子模块”, or “把 vllm / vllm-ascend remotes 改成我的 fork”. Do not use for ordinary coding, serving, benchmarking, or unrelated Git tasks.
---

# Repo Init

Prepare a fresh or drifted `vllm-ascend-workspace` clone for development.

This skill is optional. Do not treat it as a prerequisite for unrelated work.

## Outcome

A successful run leaves the local clone in a recommended but editable state:

- `gh` is installed, or the user has a prepared no-admin fallback.
- GitHub auth exists on `github.com`.
- recursive submodules are initialized.
- remotes and local tracking branches match the user’s chosen topology.
- for broad workspace init, a local workspace machine profile exists under `.vaws-local/`.
- tracked files keep community URLs; user-specific topology and machine profile state remain local runtime state.

## Use this skill when

- the user asks to initialize the workspace after clone
- the user asks to install or configure GitHub CLI
- the user asks to sign into GitHub
- the user asks to initialize recursive submodules
- the user asks to configure forks or remotes for the workspace, `vllm`, or `vllm-ascend`
- the user asks for a broad workspace init and the local machine profile is missing
- missing auth, missing recursive submodules, or obviously broken remote topology is the clear blocker

## Do not use this skill when

- the task is ordinary coding, debugging, docs, serving, or benchmarking
- the task is generic Git work unrelated to initial setup
- the user only wants remote machine attach / repair; use `machine-management` instead

## Core rules

- Be idempotent and conservative.
- Ask before any environment-changing action.
- Allow partial completion if the user declines a step.
- Preserve extra remotes such as `upstream2`.
- Never write secrets or user-specific remotes into tracked files.
- Keep local runtime state only under `.vaws-local/`.
- Prefer helper scripts in `scripts/` and `.agents/scripts/` over ad-hoc shell pipelines.

## Local runtime state

Workspace-local runtime state lives under the untracked directory `.vaws-local/`:

- `.vaws-local/machine-profile.json`
- `.vaws-local/machine-inventory.json`

The machine profile stores the stable machine username / namespace used later by `machine-management` to derive collision-safe container names such as `vaws-alice123`.

Inspect it first when the task is a broad workspace init:

```bash
python3 .agents/scripts/workspace_profile.py summary
```

If the profile is missing and the user is doing a broad init, ask once for the desired machine username:

- allowed: English letters and digits only
- normalize to lowercase
- reject symbols and spaces
- blank input means auto-generate a default such as `agent7k2p9x`

For narrow Git-only tasks, do not force profile creation.

## Script-first entry points

Start with the probe script:

- POSIX: `python3 .agents/skills/repo-init/scripts/repo_init_probe.py --compact`
- Windows: `py -3 .agents/skills/repo-init/scripts/repo_init_probe.py --compact`

Shared profile helper:

- `python3 .agents/scripts/workspace_profile.py summary`
- `python3 .agents/scripts/workspace_profile.py ensure --username <letters-or-digits>`
- `python3 .agents/scripts/workspace_profile.py ensure`  # auto-generate if missing

Prefer the topology helper for deterministic mutations and quiet comparisons:

- `python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo <path>`
- `python3 .agents/skills/repo-init/scripts/repo_topology.py configure --repo <path> [--origin-url URL] [--upstream-url URL]`
- `python3 .agents/skills/repo-init/scripts/repo_topology.py ensure-main --repo <path> --remote <origin-or-upstream>`

Open raw command recipes only if a helper script is unavailable:

- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/command-recipes.md`
- `.agents/skills/repo-init/references/acceptance.md`

## Recommended topology

Treat this as the target state when the user wants fork-based work.

| Repository | Recommended `origin` | Recommended `upstream` | Notes |
| --- | --- | --- | --- |
| workspace | user fork, if the user wants one | `maoxx241/vllm-ascend-workspace` | If already on the user repo, offer to add `upstream`. |
| `vllm` | user fork, if one exists and the user wants it | `vllm-project/vllm` | Community-only mode is valid. |
| `vllm-ascend` | user fork | `vllm-project/vllm-ascend` | Fork-based PR work is recommended. |

## Workflow

### 1. Probe first

Run the probe script and summarize only the compact facts that matter:

- whether `gh` exists
- whether GitHub auth exists
- whether SSH is usable
- whether submodules are initialized
- which forks exist
- what each repo currently uses for `origin` and `upstream`
- whether the local workspace machine profile already exists

### 2. For broad init, ensure the local machine profile early

If the request is a broad workspace init rather than a narrow Git-only task:

1. inspect `.vaws-local/machine-profile.json`
2. if it exists, reuse it
3. if it is missing, ask once for the desired machine username
4. if the user leaves it blank, auto-generate one with `workspace_profile.py ensure`

Do not rewrite an existing machine profile unless the user explicitly asked to change it.

### 3. Ask by change category

Group approvals by category instead of asking one command at a time:

- create or change the local machine profile
- install or configure `gh`
- authenticate GitHub
- initialize recursive submodules
- change remotes
- move local branches or set tracking
- sync a user fork from upstream

### 4. Ensure `gh`

Preferred install choices:

- macOS: Homebrew
- Ubuntu / Debian / WSL: official GitHub CLI Debian packages
- Windows: `winget`
- no admin: hand the user the fallback installer script already bundled in this skill

Never silently edit shell startup files or PATH.

### 5. Ensure GitHub auth

Preferred interactive flow:

```bash
gh auth login --hostname github.com --git-protocol ssh --web
```

Verify with:

```bash
gh auth status --hostname github.com
gh api user --jq .login
```

If SSH is requested and no key exists, ask before creating or uploading one.

### 6. Initialize recursive submodules

Use the recursive form for this repo:

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

### 7. Configure remotes per repo

Decide `workspace`, `vllm`, and `vllm-ascend` independently.

Use `repo_topology.py configure` for deterministic remote mutations.

Rules:

- `workspace`: add community `upstream` when the current clone is already the user repo
- `vllm`: a user fork is optional
- `vllm-ascend`: a user fork is recommended for PR-oriented work
- if a user refuses a fork, community-only mode is still a successful outcome

### 8. Compare or sync forks quietly

Do not use `git fetch --prune` merely to inspect divergence. It can emit thousands of deleted refs.

Instead:

- prefer `repo_topology.py compare-main`
- or use `git ls-remote --heads <remote> main`
- use `gh repo sync USER/REPO --source OWNER/REPO` only after explicit approval

### 9. Move local branches only when safe

Use `repo_topology.py ensure-main` for targeted `main` tracking.

Rules:

- prefer a local `main` tracking the chosen working remote’s `main`
- fetch only the branch you need and keep it quiet
- if the worktree is dirty, ask before switching branches or pulling
- do not hard reset without explicit approval

### 10. Optional `gh` default repo

When both `origin` and `upstream` exist, prefer `gh repo set-default upstream` for PR-oriented flows unless the user asks otherwise.

### 11. Finish with a compact status report

Report:

- local machine profile state when relevant
- tool install state
- auth state
- submodule state
- remote topology for each repo
- branch placement for each repo
- skipped or declined steps
- remaining manual action, if any

## Output discipline

- Prefer script JSON over long raw command output.
- Redirect noisy commands to a temp log and show only a short summary or failure tail.
- Avoid branch-wide fetches when a single-branch comparison is enough.
- Do not open large reference files unless the helper scripts are insufficient.
