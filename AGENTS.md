# Repository instructions

This repository is a composable scaffold for local `vllm` + `vllm-ascend` development. It is not a mandatory workflow engine.

## Skills available in this repo

Repo-local skills live under `.agents/skills/`.

- `repo-init`: initialize this workspace after clone, including `gh`, GitHub auth, recursive submodules, optional fork / remote topology, and the local workspace machine profile used later by machine-management.
- `machine-management`: add, verify, repair, or remove a workspace-managed remote NPU machine and its managed container.

Both skills are optional. Do not force them as a gate before normal coding, docs work, serving, benchmarking, or unrelated Git / SSH tasks.

## What belongs here vs. in a skill

Keep `AGENTS.md` focused on repo-wide rules that should apply before any task starts.

Put detailed step-by-step workflows, long examples, fallback procedures, and command recipes inside the relevant skill package instead of here.

## Repo-wide operating rules

- Never write secrets, passwords, private keys, tokens, or user-specific machine metadata into tracked files.
- Keep repo-local runtime state only under the untracked directory `.vaws-local/`.
- Treat the legacy repo-root `.machine-inventory.json` as compatibility input only.
- Keep `.gitmodules` on community URLs:
  - `https://github.com/vllm-project/vllm.git`
  - `https://github.com/vllm-project/vllm-ascend.git`
- Prefer helper scripts under `.agents/skills/*/scripts/` and `.agents/scripts/` for deterministic work.
- Prefer concise machine-readable summaries over long raw command logs.
- When a command is noisy, capture the log and report only a compact summary plus a short failure tail.
- Preserve user choices and extra remotes unless the user explicitly asks to replace them.

## Skill routing

Use `repo-init` when the user explicitly asks to:

- initialize the workspace after clone
- install or configure `gh`
- sign into GitHub
- initialize recursive submodules
- configure forks or remotes for the workspace, `vllm`, or `vllm-ascend`
- create or confirm the local workspace machine profile during a broad init

Use `machine-management` when the user explicitly asks to:

- configure or add a remote NPU server for this workspace
- check whether a managed machine is ready
- repair host SSH, container SSH, or managed-container drift
- remove a managed machine from this workspace
- create the local workspace machine profile when repo-init was skipped

Do not use `machine-management` for code sync, source rebuilds, serving, benchmarking, or generic SSH work unrelated to workspace-managed machines.

## Maintenance rule

When you change a skill, update the whole skill package together:

- `SKILL.md`
- `scripts/`
- `references/behavior.md`
- `references/command-recipes.md`
- `references/acceptance.md`

When the change affects shared local-state behavior, also update:

- `.agents/scripts/workspace_profile.py`
- `.agents/lib/vaws_local_state.py`
