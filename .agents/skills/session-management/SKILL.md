---
name: session-management
description: Create, list, inspect, remove, garbage-collect, and group isolated VAWS agent sessions, and optionally coordinate NPU task intent across independent agents on one host. Use before remote execution when tasks must not share worktrees, containers, serving state, or resource leases, when cooperative NPU queueing is requested, or when one distributed scenario needs an ordered set of existing sessions. Do not use for service lifecycle, code sync, benchmarks, or distributed failure diagnosis.
---

# Session Management

When `.vaws-local/workspace-identity.json` contains a unified alias, new
session container names inherit the base machine namespace. Session records
also snapshot `agent_id` and alias for cooperative attribution. Existing
session/container names are never rewritten after an alias change.

Create and maintain isolated VAWS sessions for parallel agent work.

Each session binds:

- one local Git worktree
- one remote session container
- one `.vaws-local/sessions/<session-id>/` state namespace
- local leases for container SSH port, service port, and optional NPU devices

A session group binds two or more ready sessions with the same code and
submodule snapshot, including content-level parity for dirty worktrees, plus
explicit startup and reverse shutdown order. Grouping
does not create another container or duplicate member leases.

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
- For NPU work, reserve devices during creation with `--devices` or `--npu-count`; session-aware serving uses that lease by default. `--npu-count` requires a successful host NPU probe (no guessing of device ranges); if the probe fails, fix it or pass explicit `--devices`.
- `--reuse-existing` probes the existing container's SSH endpoint before reporting the session as reusable; a dead container returns `needs_repair` instead of a stale `ready`.
- Metadata status changes never release remote leases. Confirmed container removal releases its leases even without `--release-leases`; worktree removal or stopping only the recorded service PID does not prove all remote resources are free.
- Managed serving requires a nonempty live NPU lease matching the session snapshot. Empty or stale snapshots cannot fall through to idle-card selection.
- `session_remove.py --remove-worktree` auto-forces removal of a *clean* worktree (git always demands `--force` for submodule-containing worktrees); a worktree with local changes still requires an explicit `--force`.
- Consumer entry points (parity, serving, benchmark, profiling-collection, memory-profiling, profiling-analysis) auto-resolve the session by walking up from the current working directory to the nearest `.vaws-local/current-session.json` worktree binding, so running them from inside the session worktree needs no target arg. Pass `--session-id <id>` or `--session-file <session.json>` only when running outside the worktree or targeting a different session.
- Domain skill commands (serving, benchmark, profiling) are session-only; they have no `--machine` surface. `--machine` exists only on `session_create.py` (selects the base machine) and in machine-management (registration/verification).
- Session removal should stop only that session's service and release only that session's leases.
- Shared NPU coordination is an optional gentleman's agreement. It must not become a mandatory gate for existing serving, benchmark, profiling, or remote-command flows.
- Shared coordination state is intentionally ephemeral under the remote host's `/tmp`; if it disappears, start a new coordination epoch and trust actual host occupancy over missing declarations.

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
python3 .agents/skills/session-management/scripts/session_diff.py
python3 .agents/skills/session-management/scripts/session_remove.py --session-id <id> --remove-container --remove-worktree --release-leases
python3 .agents/skills/session-management/scripts/session_gc.py
python3 .agents/skills/session-management/scripts/session_gc.py --reap-dead --apply
```

`session_gc.py` reports stale metadata without releasing leases by default.
`--reap-dead --apply` releases only after the host confirms an absent/stopped
container and repeated NPU probes confirm its leased cards free. Missing or
unreadable metadata, an old `removed` status, refused SSH connections, auth
failures, and timeouts all retain leases until remote ownership is resolved.

Optional cross-agent NPU coordination on the same bare-metal host:

```bash
python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine <alias-or-ip> submit \
  --task-id <id> --npu-count 2 \
  --estimated-duration-seconds 1800

python3 .agents/skills/session-management/scripts/npu_coordination.py \
  --machine <alias-or-ip> acquire --task-id <id>
```

The coordinator uses `/tmp/vaws-npu-coordinator/v1/coordinator.sqlite3` on the
bare-metal host. It is advisory, does not alter existing local Session leases,
and never stops an observed external or human process. It automatically
publishes the persistent workspace UUID plus configured agent alias;
`--agent-id` and `--agent-alias` remain explicit overrides.

```bash
python3 .agents/skills/session-management/scripts/session_group.py create \
  --group-id <id> \
  --member <name>=<session-id> \
  --member <name>=<session-id> \
  [--startup-order <name,name,...>]

python3 .agents/skills/session-management/scripts/session_group.py status --group-id <id>
python3 .agents/skills/session-management/scripts/session_group.py list
python3 .agents/skills/session-management/scripts/session_group.py teardown \
  --group-id <id> \
  [--remove-containers] [--remove-worktrees] [--release-leases] [--force]
```

Progress is emitted on `stderr` as `__VAWS_SESSION_PROGRESS__=<json>`. Final output is JSON on `stdout`.

`session_create.py` output includes a `next_steps` array that walks the agent through the recommended follow-up: `cd` into the worktree (all skill commands auto-resolve the session from there), run `session_diff.py` to review changes, and — in Cursor — use the cursor-app-control MCP tool `move_agent_to_root` to switch the agent workspace to the worktree. Switching to the worktree with `move_agent_to_root` after creation is recommended (not enforced): it makes every subsequent skill command auto-resolve this session with no target arg.

`session_diff.py` summarizes all local changes of a session — the scaffold worktree plus each initialized submodule — against the recorded base. Run it with zero args from inside the worktree (auto-bind), or pass `--session-id <id>` / `--session-file <path>`; add `--stat` for full diffstat text. It emits one JSON object on `stdout` with `status`, `session_id`, `worktree_root`, `branch`, `base_ref`, `has_changes`, a `scaffold` object (`branch`, `head`, `base`, `uncommitted[]`, `commits[]`, `changed_files[]`, and `diffstat` when `--stat`), and a `submodules[]` array (same shape per submodule, plus `skipped` for uninitialized ones). The scaffold base is the session's `base_ref`; each submodule base is its recorded `base_commit`, falling back to the gitlink at `base_ref`.

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
- `groups/<group-id>/group.json`

Worktree bindings are written to `<worktree>/.vaws-local/current-session.json` and include the absolute base session file path so scripts run from the worktree can find the base session state. This binding is what lets consumer commands auto-resolve the session by walking up from the current working directory.

Worktree creation puts every initialized submodule (`vllm/`, `vllm-ascend/`) on branch `session/<id>` — no more detached HEAD — and records `{path, branch, base_commit}` for each under `local.submodule_branches` in the session state. `session_diff.py` uses those recorded `base_commit` values as each submodule's diff base.

Reusing a bound worktree never updates initialized children to parent gitlinks
or resets their branches. Existing commits and dirty content remain intact.
If initial creation failed partway through submodule initialization, inspect
and initialize only the missing children before retrying parity.

For explicit `--session-id --no-worktree` timing/debug sessions, `session_create.py` does not overwrite the repo-root `.vaws-local/current-session.json`; agents should pass `--session-id` or `--session-file` explicitly for those shared-root flows. Current-session binding writes are atomic so readers never observe partial JSON.

## References

- `.agents/skills/session-management/references/behavior.md`
- `.agents/skills/session-management/references/command-recipes.md`
- `.agents/skills/session-management/references/acceptance.md`
