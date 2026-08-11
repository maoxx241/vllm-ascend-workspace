# Acceptance

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
- `session_remove.py` returns `needs_repair` instead of `removed` when requested container or worktree removal fails.
- An exception during remote cleanup marks the Session `needs_repair` and keeps its leases.
- `session_gc.py` does not release leases for generic `failed` sessions.
- Legacy `--machine` commands continue to work against the base machine state.
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
