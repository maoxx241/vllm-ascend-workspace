---
name: serving
description: Use when the user wants to start service, status service, list services, stop service, or manage model-serving sessions for this workspace.
---

# Serving

## Overview

Manage explicit model-serving lifecycle on a ready machine. This skill owns service identity, reuse-vs-launch decisions, and service session lifecycle; it does not own machine attach or benchmark execution.

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

## Quick Triage

- Confirm the request is about service lifecycle, not machine attach or benchmark execution.
- Use `serving.list_sessions` or `serving.describe_session` to inspect exact reuse candidates before launching anything.
- If machine readiness is uncertain, confirm `machine.probe_host_ssh` and `runtime.probe_container_transport` before touching service lifecycle.
- Check `code_parity` and `runtime_env` before reusing or launching a service.

## Default Recipe

- Discovery family: `.agents/discovery/families/serving-lifecycle.yaml`
- Inspect existing sessions first with `serving.list_sessions` or `serving.describe_session` when reuse is plausible.
- Reuse only when the existing service identity and fingerprint still match the request.
- Launch recipe: `serving.launch_service` -> `serving.probe_readiness` -> `serving.describe_session`
- Inspection recipe: `serving.describe_session` or `serving.list_sessions`
- Stop recipe: `serving.stop_service`

## Stop Conditions

- Stop on fingerprint mismatch instead of reusing stale service state.
- Stop on unavailable transport instead of pretending launch can continue.
- Stop on readiness timeout instead of reporting a maybe-ready service.
- Stop on machine-not-ready prerequisites and hand that repair back to `machine-management`.

## User-Visible Output Contract

- Report whether the requested service session is `ready`, `needs_input`, `needs_repair`, or `blocked`.
- Identify the target machine, served model alias, reachable endpoint, and current lifecycle state.
- Say plainly whether the service was started, reused, already stopped, or rejected as mismatched.

## Auth Boundary

- Allowed: none.
- Forbidden: any Git auth prompt, server auth prompt, container auth prompt, or benchmark-time auth prompt during serving work.
- On any unexpected auth prompt, fail closed with `needs_input` or `needs_repair` and redirect machine repair to `machine-management`.

## Never Expose

- raw secret values, API keys, or private host metadata
- internal PID files, lock files, or detached process plumbing
- backend-only launch plumbing as if it were the public workflow

## Cross-Skill Boundary

- `workspace-init` owns first-time setup.
- `machine-management` owns machine attach, verify, and removal work.
- `serving` owns service launch, readiness, description, listing, reuse decisions, and stop work.
- `benchmark` owns probe execution after a suitable service exists.
- `workspace-reset` owns explicit destructive teardown.

## Common Mistakes

- Treating serving as machine maintenance.
- Launching blindly instead of checking whether reuse is exact and safe.
- Reusing a stale service session without checking the fingerprint.

## Red Flags

- claiming service readiness from machine readiness alone
- guessing a service identity from host and port without session evidence
- routing routine service lifecycle work through benchmark execution
