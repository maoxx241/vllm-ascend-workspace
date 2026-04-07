# vllm-ascend-workspace

`vllm-ascend-workspace` is a composable, agent-first scaffold for developing against three repositories at once:

- the workspace control repository
- `vllm/`
- `vllm-ascend/`

The repository is intentionally **not** a mandatory workflow. Developers can use only the pieces they want. The bundled repo-local skills are:

- `repo-init`
- `machine-management`
- `remote-code-parity`

## Design goals

- Keep the tracked repository state public-safe and community-oriented.
- Keep user-specific remotes, auth state, machine profile state, and managed-machine inventory in **local untracked state** only.
- Make initialization optional, conservative, and repeatable.
- Let agents drive setup in natural language instead of making the developer compose shell commands by hand.
- Make ready remote execution use the exact current local workspace state even when the developer has not committed or pushed yet.
- Preserve user freedom to add custom remotes such as `upstream2`, keep community-only mode, or skip any suggested step.

## Tracked repository model

This repository expects two Git submodules:

- `vllm/` -> `https://github.com/vllm-project/vllm.git`
- `vllm-ascend/` -> `https://github.com/vllm-project/vllm-ascend.git`

The tracked `.gitmodules` file should always stay pointed at the community repositories on `main`. Personal forks are a **local runtime concern**, not a tracked file concern.

## Local runtime state

Untracked workspace-local state lives under `.vaws-local/`:

- `.vaws-local/machine-profile.json`: the stable machine username / namespace used to derive collision-safe container names such as `vaws-alice123`
- `.vaws-local/machine-inventory.json`: managed remote-machine records for this local clone
- `.vaws-local/remote-code-parity/install-consents.json`: per-container approval state for the first replacement of image-provided `vllm` / `vllm-ascend`
- `.vaws-local/remote-code-parity/runtime-state.json`: advisory parity state such as the last synced synthetic commits, runtime root, and container-local cache root per managed target

The legacy repo-root `.machine-inventory.json` is compatibility input only.

## What `repo-init` does

When invoked, `repo-init` can:

1. detect the machine, shell, OS, package managers, GitHub CLI state, GitHub auth state, local workspace machine profile state, and current remote topology
2. install `gh` on macOS, Ubuntu, WSL, or Windows
3. support headless auth flows and prefer SSH when possible
4. initialize submodules recursively
5. inspect whether the logged-in user has forks of:
   - `maoxx241/vllm-ascend-workspace`
   - `vllm-project/vllm`
   - `vllm-project/vllm-ascend`
6. optionally create or adopt forks and wire local remotes into the recommended topology
7. optionally sync user forks to the latest community `main`
8. for broad workspace init, create the local machine profile that later machine-management will reuse
9. optionally place local `main` branches on the expected tracking branch for active development

`repo-init` is **idempotent** and **conservative**:
- it asks before installing tools
- it asks before logging into GitHub
- it asks before generating or uploading SSH keys
- it asks before forking repositories
- it asks before renaming or replacing remotes
- it asks before syncing forks or resetting branches
- it asks before changing the local machine profile when one already exists

If the developer declines any step, the skill should stop at the last safe state and report a partial but valid result.

## What `machine-management` does

When invoked, `machine-management` can:

1. create or reuse the local workspace machine profile when repo-init was skipped
2. bootstrap host key-based SSH with a single interactive password-authenticated step
3. probe host prerequisites
4. stop for an explicit image decision gate: `main`, `stable`, or a concrete custom image reference
5. create or repair one managed host-network container per requested machine
6. verify direct local -> container SSH and run a `torch` + `torch_npu` smoke test
6. stream bounded machine-phase progress and record the actual selected image used for that container
7. keep long `docker pull`, `apt-get update`, and package-install steps attributable with heartbeat-style progress
8. persist managed-machine state in local inventory
9. mesh managed containers together on a best-effort basis
10. remove a managed container and clean local trust state

New managed containers should derive their name from the local machine profile rather than using one global shared name.
The workflow should never default to `auto`, the moving `latest` tag, or another implicit image reference.


## What `remote-code-parity` does

When invoked automatically before remote execution, `remote-code-parity` can:

1. treat the local working tree as the source of truth, including committed, staged, unstaged, and untracked **non-ignored** files
2. build synthetic Git snapshot commits for the workspace root, `vllm/`, `vllm-ascend/`, and any nested populated submodules without forcing a real commit
3. push those snapshots directly into **container-local** bare mirror repositories over direct local -> container SSH
4. publish an advertised current branch inside each mirror so nested repos can fetch the synthetic snapshot cleanly
5. materialize the mirrored commits in place inside `/vllm-workspace` so the checked-out code matches the local workspace exactly
6. preserve runtime-private paths such as `Mooncake` instead of replacing the entire runtime root
7. on the first sync for a fresh container, require explicit user consent before uninstalling image-provided `vllm` / `vllm-ascend`, deleting only `/vllm-workspace/vllm` and `/vllm-workspace/vllm-ascend`, and reinstalling them from the synced source trees
8. on later syncs, reinstall only when build-critical paths changed
9. verify the final runtime commit ids instead of assuming success from command exit status alone

`remote-code-parity` is not a machine-attach or SSH-repair workflow. It assumes `machine-management` already proved the target container is reachable by key-based SSH.

## Recommended remote topology

The skills treat the following as the recommended end state, but never as a hard requirement.

| Repository | Recommended `origin` | Recommended `upstream` | Notes |
| --- | --- | --- | --- |
| workspace | user fork, if the user wants one | `maoxx241/vllm-ascend-workspace` | If the clone is already the user fork, offer to add `upstream`. |
| `vllm` | user fork, if one exists and the user wants it | `vllm-project/vllm` | Community-only mode is valid. |
| `vllm-ascend` | user fork | `vllm-project/vllm-ascend` | A personal fork is recommended for PR-oriented work. |

A user may keep extra remotes such as `upstream2`; `repo-init` must preserve them.

## Recommended branch placement

The skills should optimize for the developer-facing state instead of preserving a detached submodule checkout forever.

- `workspace`: keep the current branch unless the user explicitly wants branch movement.
- `vllm`: prefer a local `main` tracking the chosen working remote's `main`.
- `vllm-ascend`: prefer a local `main` tracking the chosen working remote's `main`.

If the relevant worktree is dirty, the skill must ask before changing branches, rebasing, syncing, or resetting.

## Quick usage

Example prompts for an agent:

- “Initialize this workspace for vLLM Ascend development.”
- “Run repo-init and set me up for PR work.”
- “Only install GitHub CLI and log me in. Do not touch remotes.”
- “Initialize submodules recursively and move `vllm-ascend` to my fork.”
- “Run repo-init in community-only mode. Do not create any forks.”
- “Configure this NPU machine for the current workspace.”
- “Check whether the managed machine is ready.”
- “Repair the container SSH on the managed host.”
- “On the ready machine, sync my latest local code before you launch the remote service.”

## Repository layout

```text
.
├── .agents/
│   ├── README.md
│   ├── lib/
│   │   └── vaws_local_state.py
│   ├── scripts/
│   │   └── workspace_profile.py
│   └── skills/
│       ├── machine-management/
│       │   ├── SKILL.md
│       │   ├── references/
│       │   └── scripts/
│       ├── remote-code-parity/
│       │   ├── SKILL.md
│       │   ├── references/
│       │   └── scripts/
│       └── repo-init/
│           ├── SKILL.md
│           ├── references/
│           └── scripts/
├── .gitmodules
├── AGENTS.md
└── README.md
```

## Source of truth

- Repo-local skills live under `.agents/skills/`.
- Shared local-state helpers live under `.agents/lib/` and `.agents/scripts/`.
