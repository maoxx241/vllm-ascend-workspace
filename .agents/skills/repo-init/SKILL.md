---
name: repo-init
description: Initialize this workspace after clone. Use for requests like “初始化仓库”, “配置 gh / GitHub 登录”, “初始化子模块”, “uv sync 外部包”, or “把 vllm / vllm-ascend remotes 改成我的 fork”. Do not use for ordinary coding, serving, benchmarking, or unrelated Git tasks.
---

# Repo Init

Prepare a fresh or drifted `vllm-ascend-workspace` clone for development.

This skill is optional. Do not treat it as a prerequisite for unrelated work.

## Use this skill when

- the user asks to initialize the workspace after clone
- the user asks to install or configure `gh`
- the user asks to sign into GitHub
- the user asks to initialize recursive submodules
- the user asks to install the workspace packages (`uv sync`) or check capabilities (`vaws_deps.py doctor`)
- the user asks to configure forks or remotes for the workspace, `vllm`, or `vllm-ascend`
- the user asks for a broad workspace init and the local machine profile is missing

## Do not use this skill when

- the task is ordinary coding, debugging, docs, serving, or benchmarking
- the task is generic Git work unrelated to initial setup
- the user only wants remote machine attach / repair; use `machine-management` instead

## Critical rules

- Probe first.
- After the user has authorized init, run the default path once: reuse existing profile, remotes, and identity. Ask only for choices that are actually missing and affect the result (missing machine username, pending alias, unset remotes).
- Preserve extra remotes such as `upstream2`.
- Never write secrets or user-specific remotes into tracked files.
- Keep local runtime state only under `.vaws-local/`.
- Silently create `.vaws-local/workspace-identity.json` with one persistent UUID4 during broad-init probing. This idempotent local bootstrap is the only allowed pre-checkpoint mutation.
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

Required package plane (do not re-derive capabilities; use `doctor`'s report):

- `uv sync`
- `python3 .agents/scripts/vaws_deps.py doctor`
- `python3 .agents/scripts/vaws_deps.py sync`

Reference files:

- `.agents/skills/repo-init/references/behavior.md`
- `.agents/skills/repo-init/references/command-recipes.md`
- `.agents/skills/repo-init/references/acceptance.md`

## Decision checkpoint (missing choices only)

After the probe, reuse what already exists. Do not re-ask mutation categories the user already authorized.

Ask only when the answer is missing and changes the result:

1. unified workspace alias when the identity decision is still pending
   - use the selected/existing machine username (recommended)
   - custom alias
   - no alias
   - custom mode requires one follow-up question for the literal alias
2. machine username when `.vaws-local/machine-profile.json` is missing
   - ask exactly these three options: `git-username`, `random`, `custom`
   - allowed usernames are English letters and digits only
   - normalize usernames to lowercase
   - reject spaces and symbols
   - random mode means `agent#####`
   - custom mode is not complete until the user provides the literal username in a second question

Authorized broad init defaults, unless the user overrides them:

- keep current remotes when they already exist; otherwise recommended fork mode
- initialize submodules now
- run `uv sync` now — **required for remote-dev / coordinator / knowledge work**
- CI-pinned vllm alignment after submodule init

If the user only asked for a narrow GitHub auth / `gh` task, skip the machine-profile and version-alignment questions. Advanced topology / skip-sync / keep-current overrides remain available.

## Recommended topology

Treat this as the target only after the user approves it.

| Repository | Recommended `origin` | Recommended `upstream` | Notes |
| --- | --- | --- | --- |
| workspace | user fork, if the user wants one | `vllm-ascend-workspace/vllm-ascend-workspace` | Canonical scaffold is the organization repository (public, non-fork). If already on a user fork, offer to add `upstream`. |
| `vllm` | user fork, if one exists and the user wants it | `vllm-project/vllm` | Community-only mode is valid. A matching personal fork is reported, not assumed writable, and not selected merely because it exists. |
| `vllm-ascend` | user fork | `vllm-project/vllm-ascend` | Fork-based PR work is recommended. Same personal-fork rule: report the matching fork, keep community upstream, and do not infer write permission. |

Keep-current and community-only remain valid topology modes. Do not rewrite established remotes to flatten fetch/push/protocol/pushurl/extra-remote values. `repo_topology.py configure` is for explicit fresh setup, not a transfer migration of already-configured remotes. If a legacy personal URL redirects, report the resolved repository identity; do not invent a personal fork from the redirect.

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
- use its fixed three-option payload for the username part of the grouped checkpoint
- if the user chose `git-username`, run `repo_init_profile.py apply --choice git-username`
- if the user chose `random`, run `repo_init_profile.py apply --choice random`
- if the user chose `custom`, ask one extra free-text question and only then run `repo_init_profile.py apply --choice custom --custom-username ...`

Do not silently fall back from `custom` to the detected Git username.

The probe silently ensures the UUID. If the alias decision is pending, use the
`alias_question` payload and persist the approved choice with `apply-alias`.
Choosing `none` is a durable decision and must not be asked again on each init.

### 3. Ask only for missing choices, then apply defaults

Do not mutate in the same step as the first probe summary when a required choice is still missing (username, alias).

If the request was “初始化仓库” and the profile/remotes already exist, reuse them and run the default submodule + `uv sync` path without a second round of category confirmations.

### 3a. Align vllm submodule version after submodule init

After recursive submodule init completes, if the user chose CI-pinned alignment:

- Extract the CI-pinned vllm ref with `python3 .agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py --vllm-ascend-dir vllm-ascend`.
- Prefer `vllm-ascend/.github/vllm-main-verified.commit` when present. Older checkouts may only expose a `vllm_version` workflow matrix or `vllm-ascend/docs/source/conf.py`; treat those as fallbacks and report which source was used.
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
10. required package install (`uv sync` or `python3 .agents/scripts/vaws_deps.py sync`) when the user approved it. Do not skip this for a "complete" workspace; without it, `remote_dev` and `vaws_coordinator` cannot be imported.

### 4b. Report workspace capabilities from `doctor`

After the approved mutations run:

```
python3 .agents/scripts/vaws_deps.py doctor
```

Read `extensions.capability_report.capabilities`. Name exactly which capabilities are available and which are not, using that report. Do not re-derive capability logic. A skipped `uv sync` is a successful `repo-init` only for local documentation and Git work; package-dependent capabilities stay unavailable.

### 5. Finish compactly

Report:

- machine profile result
- `gh` / auth result
- submodule result
- remote topology result for each repo
- package install result (`uv sync` / `vaws_deps.py sync`)
- capabilities the user does and does not have, copied from `doctor`
- any remaining choice the user deferred
