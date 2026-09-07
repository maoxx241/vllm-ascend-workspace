---
name: repo-init
description: Initialize workspace tooling, GitHub auth, submodules, or fork topology. Use for 初始化仓库; not ordinary coding or remote NPU machine setup.
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
- Reuse explicit user choices and authorization; ask only for unresolved choices in the requested scope.
- Preserve extra remotes such as `upstream2`.
- Never write secrets or user-specific remotes into tracked files.
- Keep local runtime state only under `.vaws-local/`.
- Silently create `.vaws-local/workspace-identity.json` with one persistent UUID4 during broad-init probing. This idempotent local bootstrap does not choose an alias or authorize other changes.
- Prefer helper scripts in `scripts/` and `.agents/scripts/` over ad-hoc shell pipelines.
- During broad init, do not call `workspace_profile.py ensure` directly for a missing profile. Use `repo_init_profile.py`.
- When the profile is missing and the user has not supplied a username choice, offer:
  - current Git username
  - random `agent#####`
  - custom username
- If the user selects custom without a literal username, ask for that missing value before profile creation.
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
- `python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply-alias --choice machine-username|none`
- `python3 .agents/skills/repo-init/scripts/repo_init_profile.py apply-alias --choice custom --custom-alias <letters-or-digits>`
- `python3 .agents/scripts/workspace_identity.py summary|ensure|set-alias|decline-alias`

Low-level shared profile helper, mainly for maintenance and debugging:

- `python3 .agents/scripts/workspace_profile.py summary`
- `python3 .agents/scripts/workspace_profile.py validate <letters-or-digits>`
- `python3 .agents/scripts/workspace_profile.py ensure --username <letters-or-digits>`
- `python3 .agents/scripts/workspace_profile.py ensure --generate`

Topology helper:

- `python3 .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo <path>`
- `python3 .agents/skills/repo-init/scripts/repo_topology.py configure --repo <path> [--origin-url URL] [--upstream-url URL] [--gh-default origin|upstream|none]`
- `python3 .agents/skills/repo-init/scripts/repo_topology.py ensure-main --repo <path> --remote <origin-or-upstream>`

CI-pinned vLLM resolver:

- `python3 .agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py --vllm-ascend-dir vllm-ascend`

Reference files:

- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/command-recipes.md`
- `.agents/skills/repo-init/references/acceptance.md`

## Resolve missing choices

After the probe, reuse explicit choices in the request, conversation, and saved
state. For broad init, ask one grouped question for only the unresolved items
that affect the requested work:

- Unified alias, if pending: machine username, custom alias, or no alias.
- Machine username, if missing: `git-username`, `random` (`agent#####`), or
  `custom`. The wrapper's plan payload provides these options. Custom names
  require a literal value; ask a follow-up only if it was not already supplied.
  Normalize to lowercase letters and digits; reject spaces and symbols.
- Repo topology: keep current remotes, fork mode, or community-only mode.
- Submodule initialization and, when initialization/alignment is requested,
  vLLM alignment: CI-pinned, upstream main, or keep current. Resolve CI-pinned
  from `resolve_vllm_ci_pin.py`; do not silently switch an existing checkout.

Generic “initialize the repo” does not select a username, alias, or fork.
A narrow GitHub auth / `gh` request does not require those decisions. Already
specified choices can proceed after the probe without another approval round.

## Recommended topology

Use this topology when selected by the user, including in the initial request.

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
- the persistent agent UUID and whether the unified-alias decision is still pending

### 2. Resolve the machine-profile branch when relevant

If the request is broad init and the profile is missing:

- run `repo_init_profile.py plan`
- use its three-option payload only if the username choice remains unresolved
- if the user chose `git-username`, run `repo_init_profile.py apply --choice git-username`
- if the user chose `random`, run `repo_init_profile.py apply --choice random`
- if the user chose `custom`, use the supplied literal username with `repo_init_profile.py apply --choice custom --custom-username ...`; ask only if the value is missing

Do not silently fall back from `custom` to the detected Git username.

The probe silently ensures the UUID. If the alias decision is pending, use the
`alias_question` payload and persist the approved choice with `apply-alias`.
Choosing `none` is a durable decision and must not be asked again on each init.

### 3. Resolve outstanding decisions

Apply already authorized choices after the probe. Ask for unresolved choices
before their dependent mutations; continue independent authorized work.

If the request was just “初始化仓库” or similarly broad, do not silently assume a generated username or the recommended remotes.

### 3a. Align vllm submodule version after submodule init

After recursive submodule init completes, if the user chose CI-pinned alignment:

- Extract the CI-pinned vllm ref with `python3 .agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py --vllm-ascend-dir vllm-ascend`.
- Prefer `.github/vllm-main-verified.commit` when present. Older checkouts may only expose a `vllm_version` workflow matrix or `docs/source/conf.py`; treat those as fallbacks and report which source was used.
- Check out `vllm/` at that commit.
- Report the active version combination (vllm commit + vllm-ascend branch) in the finish summary.

### 4. Apply approved changes in order

Execute categories in the order listed below. **Submodule init must complete before remote rewiring of submodule repos**, because uninitialized submodule directories are not independent git repositories — running `repo_topology.py configure --repo <submodule>` on an uninitialized submodule will silently resolve to the parent workspace repo and corrupt its remotes.

1. local machine profile creation or change, then the approved alias decision
2. `gh` install / configure
3. GitHub auth
4. recursive submodule init (`git submodule sync --recursive && git submodule update --init --recursive`)
5. vllm submodule version alignment (CI-pinned checkout, if chosen)
6. remote rewiring for workspace repo
7. remote rewiring for `vllm` and `vllm-ascend` submodule repos (only after step 4)
8. branch tracking updates
9. optional fork sync

### 5. Finish compactly

Report:

- machine profile result
- `gh` / auth result
- submodule result
- remote topology result for each repo
- any remaining choice the user deferred
