---
name: session-management
description: Create, list, inspect, remove, and garbage-collect isolated VAWS agent sessions. Use before remote execution when multiple agent tasks must run in parallel without sharing local worktrees, remote containers, serving state, or resource leases.
---

# Session Management

Create and maintain isolated VAWS sessions for parallel agent work.

Each session binds:

- one local Git worktree
- one remote session container
- one `.vaws-local/sessions/<session-id>/` state namespace
- local leases for container SSH port, service port, and optional NPU devices

## Use This Skill When

- a user wants multiple agents/tasks to run in parallel
- a remote execution task should avoid interfering with another service or benchmark
- a task needs a dedicated worktree plus dedicated remote runtime/container
- you need to list, inspect, remove, or clean up existing sessions

## Critical Rules

- Prefer `--session-id`, `VAWS_SESSION_ID`, or `VAWS_AGENT_SESSION_ID` when an upstream agent already has a stable id.
- `session_create.py` creates a fresh generated id when no explicit/env id is provided; it does not reuse `.vaws-local/current-session.json` as a creation default.
- Existing-session lookup commands may use `.vaws-local/current-session.json` as a convenience fallback.
- Do not reuse the base machine container for new parallel tasks. New tasks should use `session_create.py`.
- For NPU work, reserve devices during creation with `--devices` or `--npu-count`; session-aware serving uses that lease by default.
- For session work, pass `--session-id <id>` or `--session-file <session.json>` to parity, serving, benchmark, profiling-collection, memory-profiling, and profiling-analysis entry points.
- Never call legacy `serve_stop.py --machine <alias>` from a session-scoped task.
- Session removal should stop only that session's service and release only that session's leases.

## Entry Points

```bash
python3 .agents/skills/session-management/scripts/session_create.py \
  --machine <alias-or-ip> \
  [--session-id <id>] \
  [--base-ref main] \
  [--branch session/<id>] \
  [--devices 0,1] \
  [--npu-count 2] \
  [--verification-mode ssh|full] \
  [--disable-prepared-image-cache]
```

```bash
python3 .agents/skills/session-management/scripts/session_list.py
python3 .agents/skills/session-management/scripts/session_status.py --session-id <id>
python3 .agents/skills/session-management/scripts/session_remove.py --session-id <id> --remove-container --remove-worktree --release-leases
python3 .agents/skills/session-management/scripts/session_gc.py
```

Progress is emitted on `stderr` as `__VAWS_SESSION_PROGRESS__=<json>`. Final output is JSON on `stdout`.

By default session creation uses a host-local prepared image cache keyed by the selected base image id. The first session for a base image may still install container SSH packages, then commits `vaws-session-prepared:<image-hash>-ssh-v2`; later sessions start from that prepared image and skip the repeated `openssh` package install and cached pip/pytest bootstrap. Use `--disable-prepared-image-cache` only when validating raw base-image bootstrap behavior.

Session creation defaults to `--verification-mode ssh`: it verifies host SSH and direct session-container SSH, then leaves NPU runtime proof to the task that actually uses the session, such as serving, benchmark, or profiling. Use `--verification-mode full` when validating a raw machine/container bootstrap and you need the extra `torch` / `torch_npu` smoke check during creation.

## State

Local untracked state lives under `.vaws-local/sessions/`:

- `index.json`
- `leases.json`
- `locks/`
- `<session-id>/session.json`
- `<session-id>/serving.json`
- `<session-id>/benchmark/`

Worktree bindings are written to `<worktree>/.vaws-local/current-session.json` and include the absolute base session file path so scripts run from the worktree can find the base session state.

For explicit `--session-id --no-worktree` timing/debug sessions, `session_create.py` does not overwrite the repo-root `.vaws-local/current-session.json`; agents should pass `--session-id` or `--session-file` explicitly for those shared-root flows. Current-session binding writes are atomic so readers never observe partial JSON.

## References

- `.agents/skills/session-management/references/behavior.md`
- `.agents/skills/session-management/references/command-recipes.md`
- `.agents/skills/session-management/references/acceptance.md`
