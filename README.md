# vllm-ascend-workspace

`vllm-ascend-workspace` is a composable, agent-first scaffold for developing against three repositories at once:

- the workspace control repository
- `vllm/`
- `vllm-ascend/`

The repository is intentionally **not** a mandatory workflow. Developers can use only the pieces they want. The only bundled skill in this package is `repo-init`, which prepares the local clone for development when the developer asks for it.

## Design goals

- Keep the tracked repository state public-safe and community-oriented.
- Keep user-specific remotes, forks, auth state, and credentials in **local** machine state only.
- Make initialization optional, conservative, and repeatable.
- Let agents drive setup in natural language instead of making the developer compose shell commands by hand.
- Preserve user freedom to add custom remotes such as `upstream2`, keep community-only mode, or skip any suggested step.

## Tracked repository model

This repository expects two Git submodules:

- `vllm/` -> `https://github.com/vllm-project/vllm.git`
- `vllm-ascend/` -> `https://github.com/vllm-project/vllm-ascend.git`

The tracked `.gitmodules` file should always stay pointed at the community repositories on `main`. Personal forks are a **local runtime concern**, not a tracked file concern.

## What `repo-init` does

When invoked, `repo-init` can:

1. detect the machine, shell, OS, package managers, GitHub CLI state, GitHub auth state, and current remote topology
2. install `gh` on macOS, Ubuntu, WSL, or Windows
3. support headless auth flows and prefer SSH when possible
4. initialize submodules recursively
5. inspect whether the logged-in user has forks of:
   - `maoxx241/vllm-ascend-workspace`
   - `vllm-project/vllm`
   - `vllm-project/vllm-ascend`
6. optionally create or adopt forks and wire local remotes into the recommended topology
7. optionally sync user forks to the latest community `main`
8. optionally place local `main` branches on the expected tracking branch for active development

`repo-init` is **idempotent** and **conservative**:
- it asks before installing tools
- it asks before logging into GitHub
- it asks before generating or uploading SSH keys
- it asks before forking repositories
- it asks before renaming or replacing remotes
- it asks before syncing forks or resetting branches

If the developer declines any step, the skill should stop at the last safe state and report a partial but valid result.

## Recommended remote topology

The skill treats the following as the recommended end state, but never as a hard requirement.

| Repository | Recommended `origin` | Recommended `upstream` | Notes |
| --- | --- | --- | --- |
| workspace | user fork, if the user wants one | `maoxx241/vllm-ascend-workspace` | If the clone is already the user fork, offer to add `upstream`. |
| `vllm` | user fork, if one exists and the user wants to use it | `vllm-project/vllm` | Community-only mode is valid. |
| `vllm-ascend` | user fork | `vllm-project/vllm-ascend` | A personal fork is recommended for PR-oriented work. |

A user may keep extra remotes such as `upstream2`; `repo-init` must preserve them.

## Recommended branch placement

The skill should optimize for the developer-facing state instead of preserving a detached submodule checkout forever.

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

## Repository layout

```text
.
├── .agents/
│   ├── README.md
│   └── skills/
│       └── repo-init/
│           ├── SKILL.md
│           ├── references/
│           └── scripts/
├── .claude/
│   └── skills/
│       └── repo-init/
├── .gitmodules
├── AGENTS.md
├── CLAUDE.md
└── README.md
```

## Source of truth

- Codex repo-local skills live under `.agents/skills/`.
- Claude project skills live under `.claude/skills/`.
- In this repository, `.agents/skills/repo-init/` is the source of truth and `.claude/skills/repo-init/` is a project adapter with matching behavior.
