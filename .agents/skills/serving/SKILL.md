---
name: serving
description: Use when the user wants to start service, status service, list services, stop service, or manage model-serving sessions for this workspace.
---

# Serving

## Overview

Manage model-serving sessions for this workspace after machine readiness exists. This skill owns service deployment shape, service session lifecycle, and online service identity without turning serving work into machine maintenance or benchmark execution.

If exact internal routing details are required after this skill is selected, see `references/internal-routing.md`.

## When to Use

### Intent Signals

- The user wants to start service for the current code and weights.
- The user wants to check status service or list services already running in this workspace.
- The user wants to stop service or clean up a stale explicit service session.
- The user wants serving work separated from benchmark execution.

### Examples Include, But Are Not Limited To:

- `把当前代码的模型服务起起来`
- `看看这个 service 现在是不是活着`
- `list services on the ready machine`
- `stop service after validation`

### Do Not Use

- First-time setup belongs to `workspace-init`.
- Machine attach, verify, and removal work belong to `machine-management`.
- Benchmark probe execution and result reporting belong to `benchmark`.
- Destructive teardown belongs to `workspace-reset`.

## User-Visible Output Contract

- Report whether the requested service session is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Identify the target machine, served model alias, reachable endpoint, and current lifecycle state.
- Say plainly whether the service was started, reused, already stopped, or rejected as mismatched.

## Auth Boundary

- Allowed: none.
- Forbidden: any Git auth prompt, server auth prompt, container auth prompt, or benchmark-time auth prompt during serving work.
- On any unexpected auth prompt, fail closed with `needs_input` or `needs_repair` and redirect machine repair back to `machine-management`.

## Never Expose

- raw secret values, API keys, or private host metadata
- internal PID files, lock files, or detached process plumbing
- backend-only launch plumbing as if it were the public workflow

## Required Capabilities

- `git_auth=ready`
- `repo_topology=ready`
- `servers.<target>.host_access=ready`
- `servers.<target>.container_access=ready`
- `servers.<target>.code_parity=ready`
- `servers.<target>.runtime_env=ready`

## Default Inference Rules

- Reuse an explicit service session only when its machine, code fingerprint, model alias, and service configuration still exactly match the request.
- Prefer a single clear service identity over implicit host-port guessing.
- Ask for missing target machine, weight location, or served model details instead of inventing them.

## Cross-Skill Boundary

- `workspace-init` owns first-time setup.
- `machine-management` owns machine attach, verify, and removal work.
- `serving` owns service session start, status, list, and stop work.
- `benchmark` consumes a temporary or explicit service session for probe execution.
- `workspace-reset` owns explicit destructive teardown.

## Failure Handling Notes

- Refuse to claim service readiness when the machine baseline is missing or broken.
- Refuse to reuse a stale or fingerprint-mismatched service session.
- Keep service lifecycle failures visible instead of collapsing them into generic machine readiness text.

## Failure Routing

- If `git_auth` or `repo_topology` is not ready, redirect to `workspace-init`.
- If `servers.<target>.host_access`, `servers.<target>.container_access`, `servers.<target>.code_parity`, or `servers.<target>.runtime_env` is not ready, redirect to `machine-management`.
- If the request is really benchmark execution rather than service lifecycle work, redirect to `benchmark`.

## Security Notes

- Never expose private hosts, secrets, or key material in public guidance.
- Keep detached process management and secret resolution inside the workspace boundary.
- Treat service session records as internal control-plane state, not as user-facing workflow steps.

## Common Mistakes

- Treating serving as machine maintenance.
- Treating serving as a generic shell wrapper.
- Reusing a stale service session without checking its exact fingerprint.

## Red Flags

- claiming service readiness from machine readiness alone
- guessing a service identity from host and port without registry evidence
- routing routine service lifecycle work through benchmark execution
