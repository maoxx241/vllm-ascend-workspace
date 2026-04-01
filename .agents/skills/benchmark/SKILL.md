---
name: benchmark
description: Use when the user wants to run a benchmark on a ready environment, inspect a benchmark preset, or execute the qwen3 35b tp4 benchmark workflow.
---

# Benchmark

## Overview

Run benchmark workflows on a ready environment using reusable workspace assets and runbooks. This skill owns benchmark execution and result reporting, not first-time setup or machine attachment.

If exact internal routing details are required after this skill is selected, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants to run benchmark execution on a ready environment.
- The user wants to run benchmark presets such as `qwen3 35b tp4`.
- The user wants a benchmark result summary instead of raw command transcripts.

### Examples Include, But Are Not Limited To:

- `跑 benchmark`
- `跑 qwen3 35b tp4 benchmark`
- `run benchmark on the ready environment`
- `run the qwen3 35b tp4 benchmark`

### Do Not Use

- First-time setup belongs to `workspace-init`.
- Machine attach, verify, and removal work belong to `machine-management`.
- Destructive teardown belongs to `workspace-reset`.

## User-Visible Output Contract

- State whether the requested benchmark could run on a ready environment.
- Report the benchmark preset, target machine, and key result summary.
- Stop and explain the blocking reason if code parity or runtime readiness is missing.

## Never Expose

- raw secret values or private host metadata
- internal cache paths or temporary benchmark staging details
- backend-only command plumbing as if it were the public workflow

## Default Inference Rules

- Reuse an existing benchmark preset and runbook when the request matches one.
- Require ready machine state and current code parity before execution.
- Ask for missing benchmark target details instead of guessing when the request is ambiguous.

## Cross-Skill Boundary

- `workspace-init` owns first-time setup.
- `machine-management` owns machine attach, verify, and removal work.
- `benchmark` owns benchmark execution after the environment is ready.
- `workspace-reset` owns explicit destructive teardown.

## Failure Handling Notes

- Refuse to run when machine readiness is missing or broken.
- Refuse to run when code parity is missing or stale.
- Report the blocking condition before any benchmark output is presented as valid.

## Security Notes

- Never expose private hosts, secrets, or key material in benchmark guidance.
- Keep remote execution details inside the workspace boundary.
- Treat benchmark presets and runbooks as the public execution surface, not raw backend commands.

## Common Mistakes

- Running benchmarks before machine readiness is verified.
- Treating benchmark execution as a generic shell wrapper.
- Reporting raw logs without a result summary.

## Red Flags

- running a benchmark on stale code
- skipping readiness checks because a machine was used earlier
- exposing backend-only execution plumbing as public instructions
