---
name: repo-init
description: Initialize this workspace after clone. Use for requests like “初始化仓库”, “配置 gh / GitHub 登录”, “初始化子模块”, or “把 vllm / vllm-ascend remotes 改成我的 fork”. Do not use for ordinary coding, serving, benchmarking, or unrelated Git tasks.
---

# Repo Init

Prepare a fresh or drifted `vllm-ascend-workspace` clone for development.

This skill is optional. Do not treat it as a prerequisite for unrelated work.

## Use this skill when

- the user asks to initialize the workspace after clone
- the user asks to install or configure `gh`
- the user asks to sign into GitHub
- the user asks to initialize recursive submodules
- the user asks to configure forks or remotes for the workspace, `vllm`, or `vllm-ascend`
- the user asks for a broad workspace init and the local machine profile is missing

## Do not use this skill when

- the task is ordinary coding, debugging, docs, serving, or benchmarking
- the task is generic Git work unrelated to initial setup
- the user only wants remote machine attach / repair; use `machine-management` instead

## Critical rules

- Probe first.
- Ask before every mutation category.
- Preserve extra remotes such as `upstream2`.
- Never write secrets or user-specific remotes into tracked files.
- Keep local runtime state only under `.vaws-local/`.
- Prefer helper scripts in `scripts/` and `.agents/scripts/` over ad-hoc shell pipelines.
- During broad init, do not call `workspace_profile.py ensure` directly for a missing profile. Use `repo_init_profile.py`.
- The machine-username checkpoint must use exactly three options when the profile is missing:
  - current Git username
  - random `agent#####`
  - custom username
- If the user selects custom, stop again and ask for the literal username before any mutation.
- Never infer a custom username from `gh` login, Git remotes, or the local OS account.

## Cross-platform launcher rule

- macOS / Linux / WSL: `python3 ...`
- Windows: `py -3 ...`

## Script-first entry points

Start with the probe script:

- POSIX: `python3 .agents/skills/repo-init/scripts/repo_init_probe.py --compact`
- Windows: `py -3 .agents/skills/repo-init/scripts/repo_init_probe.py --compact`

Public machine-profile wrapper for broad init:

- `python3 .agents/skills/repo-init/scripts/repo_init_profile.py plan`
- `python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply --choice git-username`
- `python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply --choice random`
- `python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply --choice custom --custom-username <letters-or-digits>`

Low-level shared profile helper, mainly for maintenance and debugging:

- `python3 .agents/scripts/workspace_profile.py summary`
- `python3 .agents/scripts/workspace_profile.py validate <letters-or-digits>`
- `python3 .agents/scripts/workspace_profile.py ensure --username <letters-or-digits>`
- `python3 .agents/scripts/workspace_profile.py ensure --generate`

Topology helper:

- `python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo <path>`
- `python3 .agents/skills/repo-init/scripts/repo_topology.py configure --repo <path> [--origin-url URL] [--upstream-url URL]`
- `python3 .agents/skills/repo-init/scripts/repo_topology.py ensure-main --repo <path> --remote <origin-or-upstream>`

Reference files:

- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/command-recipes.md`
- `.agents/skills/repo-init/references/acceptance.md`

## Mandatory decision checkpoint

After the probe and before any mutation, stop once and ask a grouped question whenever the task is broad init or remote topology changes are in scope.

That checkpoint must cover:

1. machine username choice when `.vaws-local/machine-profile.json` is missing
   - ask exactly these three options: `git-username`, `random`, `custom`
   - allowed usernames are English letters and digits only
   - normalize usernames to lowercase
   - reject spaces and symbols
   - random mode means `agent#####`
   - custom mode is not complete until the user provides the literal username in a second question
2. repo topology choice
   - keep current remotes
   - recommended fork mode
   - community-only mode
3. whether to initialize submodules now

If the user only asked for a narrow GitHub auth / `gh` task, skip the machine-profile question.

## Recommended topology

Treat this as the target only after the user approves it.

| Repository | Recommended `origin` | Recommended `upstream` | Notes |
| --- | --- | --- | --- |
| workspace | user fork, if the user wants one | `maoxx241/vllm-ascend-workspace` | If already on the user repo, offer to add `upstream`. |
| `vllm` | user fork, if one exists and the user wants it | `vllm-project/vllm` | Community-only mode is valid. |
| `vllm-ascend` | user fork | `vllm-project/vllm-ascend` | Fork-based PR work is recommended. |

## Workflow

### 1. Probe

Run the compact probe and summarize only the facts that matter:

- whether `gh` exists
- whether GitHub auth exists
- whether submodules are initialized
- which forks exist
- what each repo currently uses for `origin` and `upstream`
- whether the local machine profile exists and whether user choice is still required

### 2. Resolve the machine-profile branch when relevant

If the request is broad init and the profile is missing:

- run `repo_init_profile.py plan`
- use its fixed three-option payload for the username part of the grouped checkpoint
- if the user chose `git-username`, run `repo_init_profile.py apply --choice git-username`
- if the user chose `random`, run `repo_init_profile.py apply --choice random`
- if the user chose `custom`, ask one extra free-text question and only then run `repo_init_profile.py apply --choice custom --custom-username ...`

Do not silently fall back from `custom` to the detected Git username.

### 3. Stop for the decision checkpoint

Do not mutate in the same step as the first probe summary for broad init.

If the request was just “初始化仓库” or similarly broad, do not silently assume a generated username or the recommended remotes.

### 4. Apply approved changes by category

Typical categories:

- local machine profile creation or change
- `gh` install / configure
- GitHub auth
- recursive submodule init
- remote rewiring
- branch tracking updates
- optional fork sync

### 5. Finish compactly

Report:

- machine profile result
- `gh` / auth result
- submodule result
- remote topology result for each repo
- any remaining choice the user deferred
