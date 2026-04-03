---
name: workspace-init
description: Use when the user wants first-time setup, recovery setup after reset or partial failure, or to establish this repo's local foundation with an optional first-machine baseline.
---

# Workspace Init

## Overview

Prepare this workspace for development as one user-visible baseline. `workspace-init` owns first-time setup and recovery setup, with local foundation probes first and optional first-machine handling only when the user explicitly asked for a first machine.

## When to Use

### Intent Signals

- The user wants first-time setup for this workspace.
- The user wants to prepare this repo for development.
- The user wants the repo foundation established first and an optional first machine checked afterward.
- The user wants recovery setup after reset or partial failure.

### Examples Include, But Are Not Limited To:

- `初始化这个 workspace`
- `把这个仓库准备成可开发状态`
- `先把仓库基础配好，再看第一台机器`
- `prepare this repo for development`

## Quick Triage

- Start with `workspace.probe_config_validity`, `workspace.probe_git_auth`, `workspace.probe_repo_topology`, and `workspace.probe_submodules`.
- Use `workspace.diagnose_workspace` when local foundation probes disagree or only partial residue is visible.
- If the user asked for a first machine, inspect inventory first with `machine.describe_server` or `machine.list_servers` before inventing new machine state.
- Treat optional first-machine setup as a second stage after the local foundation is ready.

## Default Recipe

- Discovery families: `.agents/discovery/families/workspace-foundation.yaml`, `.agents/discovery/families/workspace-diagnostics.yaml`, and when a first machine is requested `.agents/discovery/families/machine-inventory.yaml` plus `.agents/discovery/families/machine-runtime.yaml`
- Local baseline ladder: `workspace.probe_config_validity` -> `workspace.probe_git_auth` -> `workspace.probe_repo_topology` -> `workspace.probe_submodules` -> `workspace.describe_repo_targets`
- Diagnose ambiguous or partial local failures with `workspace.diagnose_workspace`.
- Optional first-machine ladder when the user explicitly wants it: `machine.register_server` -> `machine.probe_host_ssh` -> `runtime.probe_container_transport`
- Repair the first-machine path only after failing probes: `machine.bootstrap_host_ssh` -> `machine.sync_workspace_mirror` -> `runtime.reconcile_container` -> `runtime.bootstrap_container_transport`

## Stop Conditions

- Stop on missing git identity instead of pretending the workspace is ready.
- Stop on missing machine inventory or machine auth when the user explicitly asked for a first machine.
- Stop on partial first-machine readiness; do not downgrade it into a fake local-only success.

## User-Visible Output Contract

- Report whether initialization is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Explain whether Git setup and any requested first machine setup are complete.
- Say plainly what missing input or repair step is still preventing a usable development baseline.

## Auth Boundary

- Allowed: GitHub login bootstrap, plus an optional first-machine bare-metal server password bootstrap when init is explicitly establishing the first machine.
- Forbidden: server password prompts outside the optional first-machine path, repeated GitHub login prompts after success, and any container auth prompt.
- On any unexpected auth prompt, fail closed with `needs_input` or `needs_repair`.

## Never Expose

- raw secret values or secret-bearing handles
- internal overlay mutation steps
- private hosts, private filesystem paths, or private cache locations

## Cross-Skill Boundary

- `workspace-init` owns first-time setup, recovery setup, and the decision order around `git_auth`, `repo_topology`, and optional first machine setup.
- `machine-management` owns later attach, verify, repair, and removal work after the baseline exists.
- `serving` owns service session lifecycle after the machine is ready.
- `benchmark` owns benchmark execution after the environment is ready.
- `workspace-reset` owns explicit destructive teardown.

## Common Mistakes

- Treating init as a command wrapper instead of a baseline-establishing skill.
- Starting first machine work before `git_auth` and `repo_topology` are ready.
- Pretending partial setup is a complete development baseline.

## Red Flags

- routing first machine setup into later maintenance work
- claiming readiness before the requested baseline exists
- fabricating a local-only ready result when the requested first machine is still blocked
