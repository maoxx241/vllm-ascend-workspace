# Repo-init behavior reference

This file defines the durable behavior of `repo-init`.

## Core contract

- Probe first.
- Ask before each mutation category.
- Preserve user choices and extra remotes.
- Keep user-specific topology and machine profile state local, not tracked.
- Prefer helper scripts to raw shell.
- Prefer quiet single-branch comparisons over broad ref pruning.

## Local-state contract

Repo-local runtime state lives under `.vaws-local/`.

Relevant files:

- `.vaws-local/machine-profile.json`
- `.vaws-local/machine-inventory.json`

Rules:

- keep the directory untracked
- create the machine profile during broad workspace init, not for every narrow Git-only task
- machine usernames must be letters and digits only
- normalize machine usernames to lowercase
- blank / default is valid only after the user explicitly accepts the default/random option
- do not rewrite an existing machine profile unless the user explicitly asked for that change
- on a missing profile, `workspace_profile.py ensure` must use either `--username` or `--generate`

## Stage model

### Stage 0: applicability

Use `repo-init` only for workspace setup, GitHub auth / CLI setup, recursive submodules, fork / remote topology, and the local machine profile during broad init.

### Stage 1: read-only probe

Use `repo_init_probe.py` to collect:

- platform and package-manager availability
- `gh` install state
- GitHub auth state and login
- local workspace machine profile state
- submodule status
- repo remote topology for `workspace`, `vllm`, and `vllm-ascend`
- whether matching personal forks appear to exist

### Stage 2: mandatory decision checkpoint

Before mutating a broad init or any topology-changing task, stop once and ask a grouped question.

That question must cover:

- machine username choice when the profile is missing
- repo topology mode: keep current, recommended fork mode, or community-only
- whether to initialize submodules now

Do not silently generate a username and do not silently rewire remotes when the user only asked for generic init.

### Stage 3: ensure local machine profile when relevant

During broad workspace init:

- inspect the profile first with `workspace_profile.py summary`
- if missing and the user chose a specific name, call `workspace_profile.py ensure --username ...`
- if missing and the user explicitly accepted the default/random option, call `workspace_profile.py ensure --generate`
- do not change an existing profile unless the user explicitly asked to change it

For narrow Git-only tasks, skip this stage.

### Stage 4: ensure tooling and auth

- Prefer official install paths when privilege exists.
- Use the bundled fallback installers when privilege does not exist.
- Verify auth with `gh auth status` and `gh api user --jq .login`.
- Prefer SSH for Git operations when feasible.

### Stage 5: submodules

Always use recursive sync + init for this repo.

### Stage 6: topology

Use `repo_topology.py configure` for remote mutations.

Rules:

- do not delete nonstandard remotes
- add `upstream` only when it helps the chosen workflow
- `vllm` user fork is optional
- `vllm-ascend` user fork is recommended but not mandatory
- if the user chose “keep current”, do not rewrite remotes just because the recommended topology differs

### Stage 7: main-branch comparison and tracking

Use `repo_topology.py compare-main` for branch-head comparison.

Use `repo_topology.py ensure-main` for local `main` tracking.

Rules:

- do not use `git fetch --prune` only to inspect divergence
- fetch only the branch that matters
- if the worktree is dirty, ask before switching branches or pulling
- do not hard reset without explicit approval

### Stage 8: optional fork sync

Only sync a user fork when the user explicitly approves it.

Preferred command:

```bash
gh repo sync USER/REPO --source OWNER/REPO
```

## Quiet-output rules

- `git fetch --prune` is too noisy for inspection because deleted fork refs can flood the transcript.
- Prefer `git ls-remote --heads <remote> main` or the helper script.
- When a command is noisy, capture it to a log and show a concise summary or short tail.

## Canonical success shape

A successful run usually ends with:

- the local machine profile present when broad init asked for it
- `gh` installed or a fallback provided
- GitHub auth valid
- recursive submodules initialized when the user approved it
- remotes matching the user’s selected topology
- local `main` tracking the selected working remote where the user approved branch movement
