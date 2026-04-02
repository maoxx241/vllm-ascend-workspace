---
name: benchmark
description: Use when the user wants to run benchmark execution against a ready environment or service session, inspect a benchmark preset, or summarize benchmark results for this workspace.
---

# Benchmark

## Overview

Run benchmark workflows against a ready machine baseline and a service session using reusable workspace assets and runbooks. This skill owns benchmark probe execution and result reporting, not first-time setup, machine attachment, or service deployment.

If exact internal routing details are required after this skill is selected, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants to run benchmark execution on a ready environment.
- The user wants to run a benchmark against a service session.
- The user wants to inspect a benchmark preset before execution.
- The user wants a benchmark result summary instead of raw command transcripts.

### Examples Include, But Are Not Limited To:

- `跑 benchmark`
- `跑 serving benchmark`
- `run benchmark on the ready environment`
- `summarize the latest benchmark result`

### Do Not Use

- First-time setup belongs to `workspace-init`.
- Machine attach, verify, and removal work belong to `machine-management`.
- Service start, status, list, and stop work belong to `serving`.
- Destructive teardown belongs to `workspace-reset`.

## User-Visible Output Contract

- State whether the requested benchmark could run on a ready environment.
- Report the benchmark preset, target machine, service session, and key result summary.
- Stop and explain the blocking reason if service, code parity, or runtime readiness is missing.

## Auth Boundary

- Allowed: none.
- Forbidden: any Git auth prompt, server auth prompt, or container auth prompt during benchmark work.
- Benchmark work may consume a temporary or explicit service session, but it must not open a new auth flow on its own.
- On any unexpected auth prompt, fail closed with `needs_input` or `needs_repair` and redirect to `workspace-init` or `machine-management` based on the failing capability.

## Never Expose

- raw secret values or private host metadata
- internal cache paths or temporary benchmark staging details
- backend-only command plumbing as if it were the public workflow

## Required Capabilities

- `git_auth=ready`
- `repo_topology=ready`
- `servers.<target>.host_access=ready`
- `servers.<target>.container_access=ready`
- `servers.<target>.code_parity=ready`
- `servers.<target>.runtime_env=ready`
- `services.<service>.lifecycle=ready` or a benchmark-temporary service session that can be created from the same ready machine baseline

## Default Inference Rules

- Reuse an existing benchmark preset and runbook when the request matches one.
- Require ready machine state and either a temporary or explicit service session before execution.
- Reuse an explicit service session only when it exactly matches the requested benchmark inputs.
- Ask for missing benchmark target details instead of guessing when the request is ambiguous.

## Cross-Skill Boundary

- `workspace-init` owns first-time setup.
- `machine-management` owns machine attach, verify, and removal work.
- `serving` owns service session start, status, list, and stop work.
- `benchmark` owns benchmark execution after the environment is ready and a suitable service session exists.
- `workspace-reset` owns explicit destructive teardown.

## Failure Handling Notes

- Refuse to run when machine readiness is missing or broken.
- Refuse to run when no suitable service session exists and a temporary one cannot be created safely.
- Refuse to run when code parity is missing or stale.
- Report the blocking condition before any benchmark output is presented as valid.

## Failure Routing

- If `git_auth` or `repo_topology` is not ready, redirect to `workspace-init`.
- If `servers.<target>.host_access`, `servers.<target>.container_access`, `servers.<target>.code_parity`, or `servers.<target>.runtime_env` is not ready, redirect to `machine-management`.
- If an explicit service session is missing, mismatched, or stale, redirect to `serving`.
- Do not retry benchmark execution through an auth prompt or an implicit setup path.

## Security Notes

- Never expose private hosts, secrets, or key material in benchmark guidance.
- Keep remote execution details inside the workspace boundary.
- Treat benchmark presets and runbooks as the public execution surface, not raw backend commands.

## Common Mistakes

- Running benchmarks before machine readiness is verified.
- Treating benchmark execution as a generic shell wrapper or as a hidden service deployment surface.
- Reporting raw logs without a result summary.

## Red Flags

- running a benchmark on stale code
- skipping readiness checks because a machine was used earlier
- assuming a machine-ready result also implies a service-ready result
- exposing backend-only execution plumbing as public instructions
