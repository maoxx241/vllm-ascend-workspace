# Repo-init acceptance criteria

## Trigger examples

These should trigger `repo-init`:

- “Initialize this workspace.”
- “Set up GitHub CLI and sign me in.”
- “Initialize submodules and point `vllm-ascend` to my fork.”
- “Configure my remotes for PR work.”

## Non-trigger examples

These should not trigger `repo-init` unless setup is the obvious blocker:

- “Fix this failing test.”
- “Explain how scheduling works.”
- “Run the benchmark suite.”
- “Update README wording.”

## Success criteria

A successful run should satisfy all applicable items below.

### Universal

- probes first before mutating
- asks before each mutation category
- allows partial completion
- never writes personal remotes or credentials into tracked files
- preserves extra remotes

### Tooling and auth

- chooses the correct platform install path for `gh`
- hands off the bundled fallback installer when privilege is missing
- verifies GitHub auth with `gh auth status` and `gh api user --jq .login`

### Submodules and remotes

- uses recursive submodule sync + init
- configures remotes only after approval
- preserves community-only mode when the user declines forks

### Branching and sync

- does not use `git fetch --prune` merely to inspect divergence
- compares `main` heads quietly
- moves local branches only after approval when worktree state makes that relevant
- syncs a user fork only after explicit approval

## Manual regression checklist

- Run `repo_init_probe.py` on a repo with uninitialized submodules.
- Reconfigure a repo that already has extra remotes and ensure they remain.
- Compare fork / upstream `main` without producing deleted-ref noise.
- Ensure `ensure-main` works on both a detached submodule checkout and an existing local `main` branch.
