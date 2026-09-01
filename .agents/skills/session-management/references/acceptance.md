# Acceptance

## Shared prepared-runtime mode

- Two authenticated clients with different management/source roots share one
  exclusive runtime and host-card authority, not two local lease registries.
- Real HTTP MCP calls verify ownership, queue progress, restart persistence,
  event cursors and rejection of unauthorized registration/release.
- Environment checkout never creates a container, installs dependencies or
  builds native code. Unknown readiness leaves the slot unavailable.
- Replying to a yield request does not change allocation state. Uncertain
  release and changed host epochs retain ownership for reconciliation.
- Hardware dual-service and K3 exact-topology acceptance remain separate from
  mocked occupancy/containers in Linux CI; see the coordinator README.

- New session records include `agent_identity.agent_id` and `agent_identity.alias` when available.
- Unified aliases participate in new session container naming through the persisted machine namespace.
- Missing identities and declined aliases preserve legacy behavior.
- Coordinator submit defaults to the persistent UUID and publishes the configured alias without requiring `--agent-id`.

- Two sessions on the same base machine have different worktree roots, container names, container SSH ports, and serving state paths.
- For non-moving image policies, session creation checks the host-local image cache before `docker pull`.
- The first session for a base image can create a `vaws-session-prepared:<image-hash>-ssh-v2` image after installing SSH packages and pip / pytest basics.
- A later session for the same base image starts from the prepared image and reports `used_prepared_image_cache: true` without reinstalling `openssh` or repeating pip / pytest bootstrap.
- `session_create.py --disable-prepared-image-cache` keeps the raw base-image bootstrap path available.
- Default session creation reports `verification_mode: ssh` and `npu_smoke_skipped: true` after host/container SSH checks.
- `session_create.py --verification-mode full` keeps the full `torch` / `torch_npu` smoke check available.
- A Session with leased NPU devices persists the exact
  `ASCEND_RT_VISIBLE_DEVICES` through Docker, the runtime profile, dedicated
  sshd, and container metadata.
- Session creation and status return `needs_repair` when the observed
  `ASCEND_RT_VISIBLE_DEVICES` differs from a non-empty lease.
- `session_create.py` without `--session-id`, `VAWS_SESSION_ID`, or `VAWS_AGENT_SESSION_ID` generates a fresh session id instead of reusing repo-root `.vaws-local/current-session.json`.
- Explicit `session_create.py --session-id <id> --no-worktree` does not overwrite the repo-root `.vaws-local/current-session.json`.
- Session container SSH port allocation does not hold the lease lock while running per-port remote SSH probes.
- `serve_start.py --session-id s1` stops only `s1`'s previous service.
- `serve_start.py --session-id s1` defaults to `s1`'s leased NPU devices and rejects explicit devices outside that lease.
- `serve_stop.py --session-id s1` does not read or mutate `.vaws-local/serving/<machine>.json`.
- `bench_run.py --session-id s2` stops only `s2`'s service at cleanup time.
- `parity_sync.py --session-id s1` derives `workspace_id=s1` and `container_identity=<s1-container>@<runtime-root>`.
- `session_remove.py --remove-container --release-leases` can skip `serve_stop.py` when no session serving state exists and still release leases after the container is removed or the stop result is `not_found`.
- `session_remove.py --remove-worktree` deinitializes populated submodules before asking Git to remove the worktree.
- `session_remove.py --remove-worktree` without `--force` refuses to remove a worktree that has local changes or ignored files (e.g. `.vaws-local/` run evidence); `--force` discards them explicitly.
- `session_create.py --reuse-existing` probes the session container SSH endpoint and reports `needs_repair` unless the probe confirms the container is alive; an inconclusive probe never surfaces the stored `ready` status.
- `session_create.py --reuse-existing` never releases the existing session's leases: local probe failures are treated as inconclusive, and lease rollback only covers leases allocated by the current run.
- `session_remove.py` returns `needs_repair` instead of `removed` when requested container or worktree removal fails.
- An exception during remote cleanup marks the Session `needs_repair` and keeps its leases.
- `session_gc.py` does not release leases for generic `failed` sessions.
- `session_create.py` output includes a `next_steps` array that instructs the agent to `cd` into the worktree, run `session_diff.py`, and (in Cursor) use the cursor-app-control `move_agent_to_root` tool to switch the agent workspace to the worktree.
- Worktree creation puts every initialized submodule (`vllm/`, `vllm-ascend/`) on branch `session/<id>` (not detached HEAD) and records `{path, branch, base_commit}` under `local.submodule_branches`.
- Consumer commands (parity, serving, benchmark, profiling-collection, memory-profiling, profiling-analysis) auto-resolve the session from the cwd worktree binding, so `--session-id` is optional when running from inside the worktree.
- `session_diff.py` with no target args auto-binds from the cwd worktree, and `--session-id` / `--session-file` select a session explicitly. Its stdout JSON reports `status`, `session_id`, `worktree_root`, `branch`, `base_ref`, `has_changes`, a `scaffold` object, and a `submodules[]` array (with `skipped` for uninitialized submodules); `--stat` adds `diffstat` text. The scaffold base is `base_ref`; each submodule base is its recorded `base_commit`, falling back to the gitlink at `base_ref`.
- Domain skill session entry points resolve their target from the cwd worktree binding (or `--session-id` / `--session-file`) rather than `--machine`; the remaining `--machine` surfaces are `session_create.py` (base machine selection), machine-management registration/verification, and the compatibility/probe entry points `parity_sync.py`, `serve_probe_npus.py`, and `npu_coordination.py`.
- A session group requires at least two unique ready sessions.
- Group creation fails when live workspace or recursive submodule snapshots differ.
- Dirty snapshots include a content digest of tracked changes, untracked files,
  and recursively dirty submodules; matching HEADs and a shared `dirty: true`
  flag are not sufficient for grouping.
- Startup order contains every member exactly once; shutdown order is its reverse.
- Group teardown delegates to `session_remove.py` for every member and retains `needs_repair` when any member fails.
- Shared NPU coordination state defaults to the bare-metal host's `/tmp/vaws-npu-coordinator/v1/` and recreates a new coordination epoch after state loss.
- Shared NPU coordination is optional and does not gate existing Session, serving, benchmark, profiling, or remote-command entry points.
- Multiple agents use SQLite transactions to grant a multi-device request atomically.
- The strict FIFO queue head is the only task eligible for a new grant.
- Actual `npu-smi` process/HBM occupancy, active manual holds, and existing cooperative grants are excluded from allocation.
- A second `preflight` probe returns a newly conflicted grant to the queue while its start window remains valid.
- Queue, grant, start, and heartbeat deadlines are reconciled without killing any process.
- A heartbeat-expired or release-requested task remains `orphaned_busy` while its devices are observed busy or occupancy is unknown.
- Releasing a task requires repeated free observations; one transient busy sample keeps the lease protected.
- An estimated duration overrun marks an active task overdue but does not release or preempt it.
- Human/manual holds can reserve exact devices for a bounded future window and report conflicts without stopping existing work.

## Native lifecycle acceptance status (2026-08-29)

This matrix is separate from the resource-layer contract checks above. It
records actual native-client results and does not treat setup, fallback calls,
or pending approvals as passes. Each client ran headless in its own
Git-initialized project with hooks installed by `vaws_client_setup.py`:

| Client | Model | Result |
| --- | --- | --- |
| Claude Code 2.1.143 | deepseek-v4-flash | New/resume/distinct and explicit child association passed; native MCP `vaws_session` passed. |
| Codex 0.147.0 | gpt-5.6-luna (effort max) | New/resume/distinct and explicit child association passed; native MCP `vaws_session` passed. |
| Cursor agent 2026.08.25 | claude-opus-5-high | New/resume/distinct and explicit child association passed; native MCP `vaws_session` passed. |
| Grok 1.0.13 | grok-4.6 | New/resume/distinct and explicit child association passed; native MCP `vaws_session` passed via the `use_tool` envelope. |
| Kimi Code 0.38.0 | default | New/resume/distinct and explicit child association passed; native MCP `vaws_session` passed after normal workspace trust (native `mcp__remote-dev__vaws_session` tool call in the wire log). Untrusted workspaces silently skip project MCP configuration. |

Earlier (2026-08-28): Claude Code and Grok passed new/resume plus remote-dev
calls; Codex's native hook path was pending and Cursor Agent was blocked by
approval prompts. Both gaps are closed by the 2026-08-29 runs above.

Three concurrent `gpt-5.6-luna` VAWS tasks used the explicit public adapter,
separate source/runtime/port/card assignments, and real bounded NPU probes. The
normal path and the subreaper rerun passed. A clean-environment setsid daemon
remained tracked, stopped safely, and restarted against an edited snapshot;
B/C retained process and source identity and continued answering NPU requests.
Manager restart preserved all three jobs. Final CPU regression: 185 passed,
plus six managed-process checks as uid 65534. No K3 model inference or production
model correctness is claimed.

# PR #66 ownership regressions

Native-task/managed-execution acceptance (separate from earlier pool evidence):

- For each of Claude Code, Grok, Kimi Code, Codex and Cursor, use actual native
  starts/resumes and MCP calls: distinct native roots in one cwd must differ,
  resume must preserve identity, and an explicit child association must share it.
- Local-only creation/source binding/finish must not initialize an HTTP client.
- The start gate must execute no user command before lease activation.
- Lost prepare/go replies and manager restarts must not launch duplicates.
- An expired heartbeat with a live CPU-initializing process must retain cards.
- Three `gpt-5.6-luna` agents must use VAWS concurrently on an authorized host,
  with actual source worktrees, distinct runtimes/ports/cards, overlapping work,
  and recorded source/process identity. Stopping one must preserve peer PIDs
  and successful peer work; then verify all owned processes and leases released.
- Configuration, schema validation, mocked hook inputs, process liveness and
  shell exit 0 are not substitutes for native-client or real NPU acceptance.

- A refused/auth-failed/unreachable container SSH endpoint retains leases.
- Missing metadata and a metadata-only `removed` status retain leases.
- Successful local worktree removal with failed service stop cannot release
  devices or mark remote resources removed.
- Reusing a bound worktree preserves committed child branches and dirty files.
- Empty or stale live NPU leases reject managed serving before stopping an
  existing service or selecting hardware.
