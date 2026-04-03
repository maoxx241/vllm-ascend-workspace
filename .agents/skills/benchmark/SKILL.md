---
name: benchmark
description: Use when the user wants to run benchmark execution against an existing ready service session, inspect a benchmark preset, or summarize benchmark results for this workspace.
---

# Benchmark

## Overview

Run benchmark execution against an explicit existing service session. This skill owns preset inspection, explicit service selection, benchmark execution, and result reporting; it does not own implicit service orchestration.

## When to Use

### Intent Signals

- The user wants to run benchmark execution against an existing service session.
- The user wants to pick or confirm a service before running benchmark execution.
- The user wants to inspect a benchmark preset before execution.
- The user wants a benchmark result summary instead of raw command transcripts.

### Examples Include, But Are Not Limited To:

- `跑 benchmark`
- `跑 serving benchmark`
- `run benchmark on the existing service session`
- `summarize the latest benchmark result`

## Quick Triage

- Inspect the workload first with `benchmark.describe_preset` when the preset choice is still unclear.
- Require an explicit `service_id`; if the user only has a machine, hand service creation back to `serving`.
- Use `serving.list_sessions` or `serving.describe_session` to confirm that the target service exists and matches the requested server.
- Confirm `code_parity` and `runtime_env` before running a benchmark against an existing service.

## Default Recipe

- Discovery families: `.agents/discovery/families/benchmark-execution.yaml` and `.agents/discovery/families/serving-lifecycle.yaml`
- Inspect preset first when needed: `benchmark.describe_preset`
- Inspect reusable services when needed: `serving.list_sessions` or `serving.describe_session`
- Inspect a previous result when needed: `benchmark.describe_run`
- Execute with explicit service identity: `benchmark.run_probe`
- If no suitable `service_id` exists, stop and hand service lifecycle work to `serving`

## Stop Conditions

- Stop on missing `service_id`.
- Stop on unknown or non-reusable `service_id`.
- Stop on stale code parity or service fingerprint mismatch instead of publishing benchmark output on stale code.
- Stop on benchmark failure after service readiness instead of reporting a partial run as valid.

## User-Visible Output Contract

- State whether the requested benchmark could run against the explicit target service.
- Report the benchmark preset, target machine, service session, and key result summary.
- Stop and explain the blocking reason if service, code parity, or runtime readiness is missing.

## Auth Boundary

- Allowed: none.
- Forbidden: any Git auth prompt, server auth prompt, or container auth prompt during benchmark work.
- Benchmark work requires an explicit reusable service session and must not open a new auth flow on its own.
- On any unexpected auth prompt, fail closed with `needs_input` or `needs_repair` and redirect to `workspace-init` or `machine-management`.

## Never Expose

- raw secret values or private host metadata
- internal cache paths or temporary benchmark staging details
- backend-only command plumbing as if it were the public workflow

## Cross-Skill Boundary

- `workspace-init` owns first-time setup.
- `machine-management` owns machine attach, verify, and removal work.
- `serving` owns explicit service lifecycle and service identity.
- `benchmark` owns preset inspection, service reuse decisions, benchmark execution, and result reporting.
- `workspace-reset` owns explicit destructive teardown.

## Common Mistakes

- Running benchmarks before readiness is checked.
- Treating benchmark execution as a hidden service deployment surface.
- Reporting raw logs without a result summary.

## Red Flags

- running a benchmark on stale code
- skipping readiness checks because the machine was used earlier
- launching or stopping a service inside benchmark execution
