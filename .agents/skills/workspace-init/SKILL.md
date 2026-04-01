---
name: workspace-init
description: Use when the user wants first-time setup, recovery setup after reset or partial failure, or to prepare this repo for development with Git setup and optional first-machine setup.
---

# Workspace Init

## Overview

Prepare this workspace for development as one user-visible capability. This includes Git setup, repo topology preparation, and optional first-machine setup without requiring the user to learn internal lifecycle terms.

If exact internal routing details are required after this skill is selected, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants first-time setup for this workspace.
- The user wants to prepare this repo for development.
- The user wants Git setup and optional first-machine setup handled together.
- The user wants recovery setup after reset or partial failure.

### Examples Include, But Are Not Limited To:

- `初始化这个 workspace`
- `把这个仓库准备成可开发状态`
- `第一次把 Git 和机器都配好`
- `prepare this repo for development`

### Do Not Use

- Later machine attach, verify, or removal work belongs to `machine-management`.
- Benchmark execution belongs to `benchmark`.
- Destructive teardown belongs to `workspace-reset`.

## User-Visible Output Contract

- Report whether initialization is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Explain whether Git setup and any requested first-machine setup are complete.
- Say plainly what missing input or repair step is still preventing a usable development baseline.

## Never Expose

- raw secret values or secret-bearing handles
- internal overlay mutation steps
- private hosts, private filesystem paths, or private cache locations

## Default Inference Rules

- Reuse an already ready Git and machine baseline when it still matches the request.
- Handle Git setup before trying to materialize a first-machine baseline.
- Stop and ask for missing required input instead of silently inventing a baseline.

## Cross-Skill Boundary

- `workspace-init` owns first-time Git setup and optional first-machine setup.
- `machine-management` owns later machine attach, verify, and removal work.
- `benchmark` owns benchmark execution after the environment is ready.
- `workspace-reset` owns explicit destructive teardown.

## Failure Handling Notes

- Do not claim the workspace is ready if Git setup is incomplete.
- Do not claim the workspace is ready if a requested first machine is still blocked or broken.
- Surface missing input and repairable problems as targeted next steps rather than generic failure text.

## Security Notes

- Never ask the user to paste raw credentials into the transcript when an existing secret handle or SSH path should be used.
- Keep secret resolution and machine auth details inside the workspace boundary.
- Never expose private hosts, tokens, or key material in normal user-facing output.

## Common Mistakes

- Treating init as a command wrapper instead of a user-visible capability.
- Splitting Git setup and first-machine setup into separate public workflows.
- Pretending partial setup is a complete development baseline.

## Red Flags

- routing first-time machine setup into a later maintenance workflow
- exposing overlay or secret details in public guidance
- claiming readiness before the requested development baseline exists
