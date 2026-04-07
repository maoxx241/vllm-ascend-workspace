# Repo-local skills

This directory contains the repository-local skill layer for Codex, Claude Code, and similar agents.

## Layout

- `.agents/skills/repo-init/` is the source-of-truth skill package for repository initialization.
- `.agents/skills/machine-management/` is the source-of-truth skill package for remote machine attach, verify, repair, and removal workflows.
- `.agents/skills/remote-code-parity/` is the source-of-truth skill package for remote code parity before remote execution.
- `.agents/scripts/workspace_profile.py` is the shared low-level helper for the local workspace machine profile.
- `.agents/lib/vaws_local_state.py` is the shared library for untracked local runtime state.
- `AGENTS.md` carries repository-wide routing rules and mandatory decision gates.

## Script-first convention

When a workflow has deterministic shell, SSH, Git, or local-state mechanics, prefer the helper script instead of rebuilding the command inline in the conversation.

Wrapper-style helpers should stream bounded phase progress on `stderr` and keep one final machine-readable JSON payload on `stdout`.

For machine-management specifically, image selection is an explicit user decision gate: choose `rc`, `main`, `stable`, or a concrete custom image reference. `rc` is the recommended developer track. Do not silently fall back to `auto`, `latest`, or another moving tag.

When you add or revise a helper script, keep the CLI alias-tolerant and give safe defaults for metadata that can be inferred. The goal is to reduce agent parameter brittleness, not to force one exact flag spelling.

Current primary helpers:

- `repo-init/scripts/repo_init_probe.py`
- `repo-init/scripts/repo_init_profile.py`
- `repo-init/scripts/repo_topology.py`
- `machine-management/scripts/machine_add.py`
- `machine-management/scripts/machine_verify.py`
- `machine-management/scripts/machine_repair.py`
- `machine-management/scripts/machine_remove.py`
- `remote-code-parity/scripts/parity_sync.py`
- `remote-code-parity/scripts/remote_code_parity.py`
- `remote-code-parity/scripts/install_consent.py`
- `remote-code-parity/scripts/gc_runtime_cache.py`
- `../scripts/workspace_profile.py`

Low-level machine-management helpers remain available for implementation work and debugging:

- `machine-management/scripts/inventory.py`
- `machine-management/scripts/manage_machine.py`

Reference files under `references/` are fallback detail, not the default execution path.

## Local runtime state

Untracked workspace-local state lives under `.vaws-local/`:

- `.vaws-local/machine-profile.json`
- `.vaws-local/machine-inventory.json`
- `.vaws-local/remote-code-parity/install-consents.json`
- `.vaws-local/remote-code-parity/runtime-state.json`

Remote-code-parity transport is container-only after machine attach: use machine inventory to resolve the target, then push synthetic refs directly into the container-local cache root. Synthetic mirrors should also publish an advertised branch ref for the current snapshot so nested repos can be materialized without brittle submodule fetch behavior. Runtime installs should keep pip mirror fallback on Tsinghua -> Aliyun -> PyPI, stream progress for long package steps, and keep consent/runtime-state writes atomic.

The legacy repo-root `.machine-inventory.json` is compatibility input only and should not be reintroduced as the primary path.

Key guardrail:

- on a missing machine profile, `workspace_profile.py ensure` now requires either `--username` or `--generate`
- broad init should normally go through `repo-init/scripts/repo_init_profile.py`, which narrows the machine-username choice to: detected Git username, random `agent#####`, or custom
- this prevents silent default usernames during broad init or first machine attach

## Maintenance rule

If you change `repo-init`, update these together:

- `.agents/skills/repo-init/SKILL.md`
- `.agents/skills/repo-init/references/`
- `.agents/skills/repo-init/scripts/`
- shared helpers when the workflow depends on local profile state

If you change `machine-management`, update these together:

- `.agents/skills/machine-management/SKILL.md`
- `.agents/skills/machine-management/references/`
- `.agents/skills/machine-management/scripts/`
- shared helpers when the workflow depends on local profile or inventory state

If you change `remote-code-parity`, update these together:

- `.agents/skills/remote-code-parity/SKILL.md`
- `.agents/skills/remote-code-parity/references/`
- `.agents/skills/remote-code-parity/scripts/`
- `AGENTS.md`, `README.md`, and this file when routing, transport model, or local-state behavior changes

Keep the files under `.agents/skills/` as the canonical supporting files for repo-local skills.

## Cursor IDE integration

Cursor IDE users: see `.cursor/rules/` for IDE-specific glob-activated rules that complement `AGENTS.md`. These rules are a thin pointer layer — they do not duplicate skill or routing content, and are designed to remain stable across submodule version switches.
