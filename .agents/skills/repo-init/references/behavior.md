# Repo-init behavior reference

This file defines the durable behavior of `repo-init`.

## Core contract

- Probe first.
- Ask before each mutation category.
- Preserve user choices and extra remotes.
- Keep user-specific topology local, not tracked.
- Prefer helper scripts to raw shell.
- Prefer quiet single-branch comparisons over broad ref pruning.

## Stage model

### Stage 0: applicability

Use `repo-init` only for workspace setup, GitHub auth / CLI setup, recursive submodules, and fork / remote topology.

### Stage 1: read-only probe

Use `repo_init_probe.py` to collect:

- platform and package-manager availability
- `gh` install state
- GitHub auth state and login
- submodule status
- repo remote topology for `workspace`, `vllm`, and `vllm-ascend`
- whether matching personal forks appear to exist

### Stage 2: summarize and ask

Before mutating, summarize only the compact state the user needs to approve the next change.

Group approvals by:

- `gh` install / upgrade
- GitHub auth
- recursive submodules
- remote rewiring
- branch movement / tracking
- fork sync

### Stage 3: ensure tooling and auth

- Prefer official install paths when privilege exists.
- Use the bundled fallback installers when privilege does not exist.
- Verify auth with `gh auth status` and `gh api user --jq .login`.
- Prefer SSH for Git operations when feasible.

### Stage 4: submodules

Always use recursive sync + init for this repo.

### Stage 5: topology

Use `repo_topology.py configure` for remote mutations.

Rules:

- do not delete nonstandard remotes
- add `upstream` only when it helps the chosen workflow
- `vllm` user fork is optional
- `vllm-ascend` user fork is recommended but not mandatory

### Stage 6: main-branch comparison and tracking

Use `repo_topology.py compare-main` for branch-head comparison.

Use `repo_topology.py ensure-main` for local `main` tracking.

Rules:

- do not use `git fetch --prune` only to inspect divergence
- fetch only the branch that matters
- if the worktree is dirty, ask before switching branches or pulling
- do not hard reset without explicit approval

### Stage 7: optional fork sync

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

- `gh` installed or a fallback provided
- GitHub auth valid
- recursive submodules initialized
- remotes matching the user’s selected topology
- local `main` tracking the selected working remote where the user approved branch movement
