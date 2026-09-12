# Repo-init behavior reference

This file defines the durable behavior of `repo-init`.

## Core contract

- Probe first.
- After authorized init, reuse existing config and ask only for missing choices that affect the result.
- Preserve user choices and extra remotes.
- Keep user-specific topology and machine profile state local, not tracked.
- Prefer helper scripts to raw shell.
- Prefer quiet single-branch comparisons over broad ref pruning.

## Local-state contract

Repo-local runtime state lives under `.vaws-local/`.

Relevant files:

- `.vaws-local/workspace-identity.json`
- `.vaws-local/machine-profile.json`
- `<primary-worktree>/.vaws-local/machine-inventory.json` (shared across linked Git worktrees)

Rules:

- keep the directory untracked
- during broad init, silently and idempotently create one UUID4 agent identity before the decision checkpoint
- ask once whether to use the machine username, a custom alias, or no unified alias; persist `declined` so the question is not repeated
- unified aliases follow the same lowercase letters/digits-only 3-32 character rule needed by container namespaces
- create the machine profile during broad workspace init, not for every narrow Git-only task
- machine usernames must be letters and digits only
- normalize machine usernames to lowercase
- the random/default format is `agent#####`
- do not rewrite an existing machine profile unless the user explicitly asked for that change
- during broad init, prefer `repo_init_profile.py` over calling `workspace_profile.py ensure` directly

## Stage model

### Stage 0: applicability

Use `repo-init` for workspace initialization or repair of its configuration and client wiring. Narrow Git/auth/dependency requests do not imply the full initialization workflow.

### Stage 1: probe plus identity bootstrap

Use `repo_init_probe.py` to collect:

- platform and package-manager availability
- `gh` install state
- GitHub auth state and login
- local workspace machine profile state
- submodule status
- repo remote topology for `workspace`, `vllm`, and `vllm-ascend`
- whether matching personal forks appear to exist

The probe may only mutate untracked local state by creating a missing
`workspace-identity.json` UUID4. It must not silently choose an alias.

### Stage 2: missing-choice checkpoint

Before mutating, ask only for choices that are still missing and affect the result:

- unified workspace alias choice when its decision is pending
- machine username choice when the profile is missing

Authorized broad init defaults: keep current remotes if present, initialize submodules, run `python .agents/scripts/vaws_deps.py sync`, CI-pinned vllm alignment. Topology / skip-sync / keep-current remain available as overrides, not as required confirmations.

If a username was not provided and a choice is needed, `repo_init_profile.py plan` offers:

- `git-username`
- `random`
- `custom`

Rules:

- do not silently generate a username when the user only asked for generic init
- do not silently rewire remotes when the user only asked for generic init
- do not treat `custom` as permission to reuse the detected Git username
- for `custom`, reuse a supplied literal; ask only if the literal is missing

### Stage 3: ensure local machine profile when relevant

During broad workspace init:

- inspect the profile first with `repo_init_profile.py plan`
- if missing and the user chose `git-username`, call `repo_init_profile.py apply --choice git-username`
- if missing and the user explicitly accepted the default/random option, call `repo_init_profile.py apply --choice random`
- if missing and the user chose `custom`, call `repo_init_profile.py apply --choice custom --custom-username ...` with the supplied literal; ask only if it is absent
- do not change an existing profile unless the user explicitly asked to change it

For narrow Git-only tasks, skip this stage.

### Stage 4: ensure tooling and auth

- Prefer official install paths when privilege exists.
- Use the bundled fallback installers when privilege does not exist.
- Verify auth with `gh auth status` and `gh api user --jq .login`.
- Prefer SSH for Git operations when feasible.

### Stage 5: submodules

Always use recursive sync + init for this repo.

When the user chose CI-pinned vLLM alignment, resolve the tested vLLM ref with
`resolve_vllm_ci_pin.py` after `vllm-ascend/` is populated. The resolver
prefers `vllm-ascend/.github/vllm-main-verified.commit`, which is the current upstream
source of truth, and falls back to older workflow/docs sources for older
checkouts. Report the resolver source in the summary so later remote install
or parity work can tell which pairing was deployed.

### Stage 5b: required package install

The three in-process packages are not submodules. After the approved
submodule work, run:

```
python .agents/scripts/vaws_deps.py sync
```

or `python3 .agents/scripts/vaws_deps.py sync`.

Rules:

- this step is required for remote-dev / coordinator / knowledge work
- the packages are public git+https installs; `uv.lock` is the only pin
- `uvx vaws-top` is a separate service and is not part of `python .agents/scripts/vaws_deps.py sync`
- do not reimplement capability logic; after install or skip, run `python3 .agents/scripts/vaws_deps.py doctor` and name available / unavailable capabilities from that report

After a successful install, `sync` calls the installed knowledge package's
`prepare --project ROOT` entry. It prepares the local model and index and reports
`knowledge.ready` independently from dependency installation. A pending model,
index or shared update does not turn a successful package install into a failure.
MCP starts internal maintenance while alive; ordinary Agent work does not call
prepare or sequence index and shared-update operations.

`python3 .agents/scripts/knowledge_setup.py` retries preparation when requested.
It preserves existing publishing choices; a new configuration enables local
knowledge and shared downloads without a fork or GitHub login. Explicit
`--contribute` configures public contribution, while `--read-only` disables it.
The package owns configuration, contribution and release state. Refresh selected
native clients to receive the service config and supported summary hooks. Hooks
reuse existing summary text; no additional Agent summary, lookup or capture is
required. Public PRs receive human review and merge.

A Windows-mounted workspace has one Windows knowledge process for Windows and
WSL clients. Preparation stays pending if its Windows interpreter is absent;
WSL does not create a second database process in the same directory. Independent
Linux workspaces keep their native Linux environment.
An unavailable knowledge service or public fork does not prevent independent
development. See the [current knowledge contract](../../../../docs/target-state.md#54-knowledge).

### Stage 6: topology

Use `repo_topology.py configure` for remote mutations.

**Prerequisite**: Stage 5 (submodule init) must be complete before configuring submodule remotes. `repo_topology.py` will refuse to operate on a path whose git root resolves to a different directory (e.g. an uninitialized submodule falling through to the parent workspace).

Rules:

- do not delete nonstandard remotes
- add `upstream` only when it helps the chosen workflow
- `vllm` user fork is optional
- `vllm-ascend` user fork is recommended but not mandatory
- matching personal forks are reported; do not treat them as community upstream, assume push access, or select them merely because they exist
- if the user chose "keep current", do not rewrite remotes just because the recommended topology differs
- `configure` is explicit fresh-setup intent, not a migration of established fetch/push/protocol/pushurl/extra remotes
- configure workspace remotes first, then submodule remotes (after submodule init)

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
- the machine profile reused a supplied/existing username, or resolved a missing choice
- `gh` installed or a fallback provided
- GitHub auth valid
- recursive submodules initialized for authorized broad init or the requested submodule setup
- remotes matching the user's selected topology
- local `main` tracking the selected working remote where the user approved branch movement
- package installation (`python .agents/scripts/vaws_deps.py sync`) completed for authorized package-dependent setup; an explicitly skipped install leaves those capabilities unavailable
- finish names capabilities from `vaws_deps.py doctor`, not from a re-derived list
